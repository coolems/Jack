"""One authenticated chat WebSocket per agentic turn.

Requests are strictly sequential: send one request, read through done/error, then the
next iteration may reuse the socket. Transport death invalidates the socket so the
bounded provider-call retry reconnects once; explicit close ends the turn's session.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("COOLEMS.Provider.CoolemsClient.ChatSession")


class UserCancelled(Exception):
    """Clean user cancellation, matching the one-shot provider modules."""


class ChatSession:
    def __init__(self, provider):
        self._provider = provider
        self._ws = None
        self._closed = False
        self._dead = True
        self._connect_count = 0
        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    @property
    def connect_count(self) -> int:
        return self._connect_count

    @property
    def closed(self) -> bool:
        return self._closed

    async def _ensure(self):
        async with self._connect_lock:
            if self._closed:
                raise ConnectionError("Chat session is closed")
            if self._ws is not None and not self._dead:
                return self._ws
            await self._invalidate()

            from . import NoServerReachable, _check_protocol, apply_auth_info, open_with_failover
            from config import PROTOCOL_VERSION

            candidates = self._provider._get_ws_candidates()
            api_key = self._provider._load_api_key()
            if not api_key:
                raise ConnectionError("No API key found for chat authentication")

            try:
                ws, ws_url = await open_with_failover(
                    candidates,
                    max_size=self._provider.max_msg_size,
                    ping_interval=self._provider.heartbeat_interval,
                    ping_timeout=self._provider.ping_timeout,
                )
            except NoServerReachable as e:
                raise ConnectionError(f"SERVER is not reachable - no configured address answered ({e})") from e
            except asyncio.TimeoutError as e:
                raise ConnectionError("Timeout connecting to chat WebSocket") from e
            except OSError as e:
                raise ConnectionError("SERVER is not reachable") from e

            try:
                await ws.send(json.dumps({
                    "type": "auth",
                    "api_key": api_key,
                    "role_type": "client",
                    "protocol_version": PROTOCOL_VERSION,
                }))
                auth_response = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=self._provider.handshake_timeout)
                )
                if auth_response.get("type") != "auth_ok":
                    raise ConnectionError(f"Chat WS auth failed: {auth_response}")
                _check_protocol(auth_response, "chat session")
                apply_auth_info(auth_response, "chat session")
            except Exception as e:
                try:
                    await ws.close()
                except Exception:
                    pass
                if isinstance(e, ConnectionError):
                    raise
                raise ConnectionError(f"Chat WS auth failed: {e}") from e

            if self._closed:
                try:
                    await ws.close()
                except Exception:
                    pass
                raise ConnectionError("Chat session is closed")

            self._ws = ws
            self._dead = False
            self._connect_count += 1
            logger.info(f"[CLIENT] Chat session established at {ws_url}")
            return ws

    async def _invalidate(self):
        self._dead = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def close(self):
        self._closed = True
        await self._invalidate()

    async def chat_stream(
        self,
        model: str,
        messages: List[Dict],
        websocket: Any,
        temperature: float = 0.7,
        # (2026-12 thinking) bool OR a canonical level ("none"/"low"/"medium"/"high").
        # Pass it through verbatim - bool() would erase the level the user picked.
        enable_thinking: Any = False,
        conv_id: str = None,
        tools: list = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Tuple[str, Any]:
        import websockets
        from app.providers import TokenStats

        async with self._request_lock:
            if stop_event and stop_event.is_set():
                raise UserCancelled("User cancelled generation.")
            ws = await self._ensure()
            if self._closed:
                raise ConnectionError("Chat session is closed")

            stop_task = None
            if stop_event:
                async def _stop_watcher():
                    await stop_event.wait()
                    logger.info(f"[CLIENT] Stop event set - closing chat session for conv={conv_id}")
                    try:
                        await ws.send(json.dumps({"type": "stop", "conv_id": conv_id or ""}))
                    except Exception:
                        pass
                    await self.close()

                stop_task = asyncio.create_task(_stop_watcher())

            final_response = None
            token_stats = TokenStats()
            msg_count = 0
            try:
                await ws.send(json.dumps({
                    "type": "request",
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "enable_thinking": enable_thinking,
                    "conv_id": conv_id or "",
                    "tools": tools if tools else [],
                }))

                while True:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=self._provider._timeout)
                    msg = json.loads(raw_msg)
                    t = msg.get("type", "")
                    msg_count += 1

                    if t == "content":
                        chunk = msg.get("content", "")
                        await websocket.send_text(json.dumps({"type": "content", "content": chunk}))
                    elif t == "thinking":
                        chunk = msg.get("content", "")
                        if chunk:
                            await websocket.send_text(json.dumps({"type": "thinking", "content": chunk}))
                    elif t == "response":
                        final_response = msg.get("content", "")
                    elif t == "token_stats":
                        try:
                            import app.license_cache as _lic
                            if msg.get("license") is not None:
                                _lic.update_from_frame(msg["license"])
                        except Exception:
                            pass
                        token_stats = TokenStats(
                            prompt_tokens=msg.get("prompt_tokens", 0),
                            generated_tokens=msg.get("generated_tokens", 0),
                            generation_speed=msg.get("generation_speed"),
                            total_duration=msg.get("total_duration", 0.0),
                            provider_name="coolems_server",
                            model=model,
                            context_window=msg.get("context_window", 131072),
                            truncated=bool(msg.get("truncated", False)),
                        )
                    elif t == "tool_calls":
                        final_response = json.dumps(msg)
                    elif t == "done":
                        logger.info(f"[CLIENT] Chat session request done after {msg_count} frames")
                        break
                    elif t == "error":
                        # Error paths may not send done; drop the socket so no stale frame
                        # can be mistaken for the next request's completion.
                        await self._invalidate()
                        raise RuntimeError(msg.get("message", "Server error"))
                    elif t == "cancelled":
                        await self._invalidate()
                        if stop_event and stop_event.is_set():
                            raise UserCancelled("User cancelled generation.")
                        raise ConnectionError("Chat request was cancelled by the server")

                return final_response if final_response is not None else "", token_stats

            except UserCancelled:
                raise
            except websockets.exceptions.ConnectionClosed as e:
                await self._invalidate()
                if stop_event and stop_event.is_set():
                    raise UserCancelled("User cancelled generation.") from None
                raise ConnectionError(f"Server connection lost: {e}") from None
            except asyncio.TimeoutError as e:
                await self._invalidate()
                raise ConnectionError("Timeout waiting for chat response") from e
            except json.JSONDecodeError as e:
                await self._invalidate()
                raise ConnectionError(f"Invalid response from chat server: {e}") from e
            except RuntimeError:
                raise
            except Exception as e:
                await self._invalidate()
                if isinstance(e, OSError):
                    raise ConnectionError("SERVER is not reachable") from e
                raise
            finally:
                if stop_task and not stop_task.done():
                    stop_task.cancel()
                    try:
                        await stop_task
                    except (asyncio.CancelledError, Exception):
                        pass