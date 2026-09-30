"""Direct WebSocket mode methods for CoolemsClientProvider.

All methods that connect directly to the server via wss:// (no relay).
"""

import asyncio
import json
import logging
from typing import List, Dict, Any, Tuple, Optional

from config import MODEL_SWITCH_PHASE1_TIMEOUT, MODEL_SWITCH_POLL_INTERVAL, \
    MODEL_SWITCH_MAX_WAIT_SEC, PROTOCOL_VERSION
from . import _check_protocol, apply_auth_info, connect_with_failover, NoServerReachable
from .client_provider import is_model_switching

logger = logging.getLogger("COOLEMS.Provider.CoolemsClient")


def _models_match(current: str, target: str) -> bool:
    """Compare two model names ignoring .gguf extension and path."""
    import os
    c = os.path.basename(current).lower()
    t = os.path.basename(target).lower()
    if c.endswith(".gguf"):
        c = c[:-5]
    if t.endswith(".gguf"):
        t = t[:-5]
    return c == t


class UserCancelled(Exception):
    """Raised when the user clicks stop — NOT an error, just a clean cancellation."""
    pass


async def chat_stream_direct_websocket(
    provider, model: str, messages: List[Dict], websocket: Any,
    temperature: float = 0.7, enable_thinking: bool = False,
    conv_id: str = None, tools: list = None,
    stop_event: Optional[asyncio.Event] = None
) -> Tuple[str, Any]:
    """Connect directly to server via encrypted WebSocket and stream chat response."""
    import websockets
    # (2026-08-20 dedup) lazy: resolves the SERVER-delivered module via PEP 562 shell
    from app.providers import TokenStats

    candidates = provider._get_ws_candidates()
    # No "connecting..." line here on purpose (2026-09-01): open_with_failover() logs the
    # single "[CLIENT] Connected to Direct WS at <url>" line once a real endpoint answers.

    try:
        async with connect_with_failover(
            candidates, max_size=provider.max_msg_size,
            ping_interval=provider.heartbeat_interval, ping_timeout=provider.ping_timeout,
        ) as ws:
            api_key = provider._load_api_key()
            await ws.send(json.dumps({"type": "auth", "api_key": api_key, "role_type": "client",
                                    "protocol_version": PROTOCOL_VERSION}))

            auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))
            if auth_response.get("type") != "auth_ok":
                raise ConnectionError(f"Direct WS auth failed: {auth_response}")

            # Protocol check + auth info (single mutation point, 2026-08-20)
            _check_protocol(auth_response, "direct WS chat")
            apply_auth_info(auth_response, "direct WS chat")

            request_payload = {
                "type": "request", "model": model, "messages": messages,
                "temperature": temperature, "enable_thinking": enable_thinking,
                "conv_id": conv_id or "", "tools": tools if tools else []
            }
            await ws.send(json.dumps(request_payload))

            response_text = ""
            final_response = None
            token_stats = TokenStats()

            stop_task = None
            if stop_event:
                async def _stop_watcher():
                    await stop_event.wait()
                    logger.info(f"[CLIENT] Stop event set — closing Direct WS for conv={conv_id}")
                    try:
                        await ws.send(json.dumps({"type": "stop", "conv_id": conv_id or ""}))
                    except Exception:
                        pass  # Connection may already be half-closed, ignore
                    # CANCEL FIX (2026-09-07): the watcher MUST close the socket itself.
                    # The old code only sent the "stop" frame and left this WS open - the
                    # main recv loop below stayed blocked in ws.recv(), so from the SERVER's
                    # point of view the client was still connected and llama.cpp generation
                    # ran to completion (the response was then just discarded). Closing here
                    # makes cancel immediate: the server sees the disconnect, sets its own
                    # stop event and aborts the in-flight generation. The recv loop wakes up
                    # with ConnectionClosed; since stop_event is set it raises UserCancelled
                    # (clean cancellation) instead of a connection error.
                    try:
                        await ws.close()
                    except Exception:
                        pass  # already closed - fine

                stop_task = asyncio.create_task(_stop_watcher())

            msg_count = 0
            try:
                while True:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=provider._timeout)
                    msg = json.loads(raw_msg)
                    t = msg.get("type", "")
                    msg_count += 1
                    logger.debug(f"[CLIENT] Direct WS frame #{msg_count}: type={t}")

                    if t == "content":
                        chunk = msg.get("content", "")
                        response_text += chunk
                        await websocket.send_text(json.dumps({"type": "content", "content": chunk}))
                    elif t == "thinking":
                        chunk = msg.get("content", "")
                        if chunk:
                            response_text += chunk
                            await websocket.send_text(json.dumps({"type": "thinking", "content": chunk}))
                    elif t == "response":
                        final_response = msg.get("content", "")
                        logger.info(f"[CLIENT] Received final response via Direct WS")
                    elif t == "token_stats":
                        token_stats = TokenStats(
                            prompt_tokens=msg.get("prompt_tokens", 0),
                            generated_tokens=msg.get("generated_tokens", 0),
                            generation_speed=msg.get("generation_speed"),
                            total_duration=msg.get("total_duration", 0.0),
                            provider_name="coolems_server", model=model,
                            context_window=msg.get("context_window", 131072),
                            truncated=bool(msg.get("truncated", False)),  # TRUNCATION FIX (2026-09-10): forward finish_reason flag
                        )
                    elif t == "done":
                        logger.info(f"[CLIENT] Received done after {msg_count} frames")
                        break
                    elif t == "tool_calls":
                        final_response = json.dumps(msg)
                        logger.info(f"[CLIENT] Received tool_calls via Direct WS")
                    elif t == "error":
                        err_msg = msg.get("message", "Server error")
                        raise RuntimeError(err_msg)
            except websockets.exceptions.ConnectionClosed as e:
                # Check if this was caused by user clicking stop — NOT an error then
                if stop_event and stop_event.is_set():
                    logger.info(f"[CLIENT] Direct WS closed due to user stop (conv={conv_id})")
                    raise UserCancelled("User cancelled generation.") from None
                else:
                    # Server disconnected without us requesting stop — this IS an issue
                    logger.warning(f"[CLIENT] Direct WS closed unexpectedly: {e}")
                    raise ConnectionError(f"Server connection lost: {e}") from None
            finally:
                if stop_task and not stop_task.done():
                    stop_task.cancel()
                    try: await stop_task
                    except (asyncio.CancelledError, Exception): pass

            result = final_response if final_response is not None else ""
            return result, token_stats

    except UserCancelled:
        # Re-raise so caller knows it was a user-initiated cancellation
        raise
    except NoServerReachable as e:
        raise ConnectionError(f"SERVER is not reachable - no configured address answered ({e})") from e
    except (ConnectionRefusedError, OSError) as e:
        raise ConnectionError("SERVER is not reachable") from e
    except asyncio.TimeoutError:
        raise ConnectionError("Timeout connecting to Direct WS")


async def health_check_direct_websocket(provider, force: bool = False):
    """Health check via direct WebSocket."""
    import websockets

    candidates = provider._get_ws_candidates()
    try:
        async with connect_with_failover(
            candidates, max_size=provider.max_msg_size,
            ping_interval=provider.heartbeat_interval, ping_timeout=provider.ping_timeout,
        ) as ws:
            api_key = provider._load_api_key()
            await ws.send(json.dumps({"type": "auth", "api_key": api_key, "role_type": "client",
                                    "protocol_version": PROTOCOL_VERSION}))
            auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))
            if auth_response.get("type") != "auth_ok":
                return (False, f"Auth failed: {auth_response}", [])

            # Protocol check + auth info (single mutation point, 2026-08-20)
            _check_protocol(auth_response, "direct health")
            apply_auth_info(auth_response, "direct health")

            await ws.send(json.dumps({"type": "health_check"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))

            if msg.get("type") == "health_ok":
                models = [m.get("name", "") for m in msg.get("models", [])]
                return (True, "", models)
            err = msg.get("message", "Unknown") if msg else "No response"
            return (False, err, [])
    except NoServerReachable as e:
        from config import COOLEMS_DIRECT_WS_PORT
        return (False, f"SERVER unavailable at any configured address ({e}) - check the server is running and firewall allows port {COOLEMS_DIRECT_WS_PORT}", [])
    except ConnectionRefusedError:
        from config import COOLEMS_DIRECT_WS_PORT
        return (False, f"SERVER unavailable - check it is running and firewall allows port {COOLEMS_DIRECT_WS_PORT}", [])
    except OSError as e:
        return (False, f"Network error: {e}", [])
    except asyncio.TimeoutError:
        return (False, "SERVER not responding - connection timed out", [])
    except Exception as e:
        return (False, str(e), [])


async def get_models_direct_websocket(provider):
    """Get models list via direct WebSocket."""
    import websockets

    candidates = provider._get_ws_candidates()
    try:
        async with connect_with_failover(
            candidates, max_size=provider.max_msg_size,
            ping_interval=provider.heartbeat_interval, ping_timeout=provider.ping_timeout,
        ) as ws:
            api_key = provider._load_api_key()
            await ws.send(json.dumps({"type": "auth", "api_key": api_key, "role_type": "client",
                                    "protocol_version": PROTOCOL_VERSION}))
            auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))
            if auth_response.get("type") != "auth_ok":
                logger.error(f"[CLIENT] Auth rejected by SERVER for models request: {auth_response}")
                return []
            _check_protocol(auth_response, "direct models")

            await ws.send(json.dumps({"type": "models_request"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))
            if msg.get("type") == "models_response":
                return msg.get("models", [])
            return []
    except (ConnectionRefusedError, OSError) as e:
        if not is_model_switching():
            logger.error(f"[CLIENT] Cannot connect to SERVER - {e}")
        return []
    except asyncio.TimeoutError:
        if not is_model_switching():
            logger.error("[CLIENT] Timeout connecting to SERVER")
        return []
    except Exception as e:
        if not is_model_switching():
            logger.error(f"[CLIENT] Get models via Direct WS failed: {e}")
        return []


async def get_current_model_direct_websocket(provider, force: bool = False):
    """Get currently loaded model via direct WebSocket.

    force=True tells the SERVER to bypass its fresh in-memory model-state cache
    and verify via /v1/models (used by the post-switch poller for immediate,
    authoritative confirmation).
    """
    import websockets

    candidates = provider._get_ws_candidates()
    try:
        async with connect_with_failover(
            candidates, max_size=provider.max_msg_size,
            ping_interval=provider.heartbeat_interval, ping_timeout=provider.ping_timeout,
        ) as ws:
            api_key = provider._load_api_key()
            await ws.send(json.dumps({"type": "auth", "api_key": api_key, "role_type": "client",
                                    "protocol_version": PROTOCOL_VERSION}))
            auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))
            if auth_response.get("type") != "auth_ok":
                return None
            _check_protocol(auth_response, "direct current_model")

            frame = {"type": "current_model"}
            if force:
                frame["force"] = True
            await ws.send(json.dumps(frame))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))
            if msg.get("type") == "current_model_response":
                model = msg.get("model", "")
                return model or None
            return None
    except Exception:
        return None


async def switch_model_direct_websocket(provider, model_filename: str):
    """Switch model on remote server via direct WebSocket.

    Two-phase approach (all budgets from CLIENT config - single source of truth):
      Phase 1 — send the command inside a bounded window (MODEL_SWITCH_PHASE1_TIMEOUT).
                If the SERVER answers quickly we get its result; if it drops the
                connection or stays silent while shutting down for the reload, that is
                EXPECTED and we simply move to Phase 2.
      Phase 2 — silently poll health + verify correct model loaded until
                MODEL_SWITCH_MAX_WAIT_SEC. (Periodic "waiting" progress lines are logged by
                agent.py's broadcaster so there is exactly ONE progress line per interval.)

    NOTE: All UI broadcasts happen in agent.py on the main event loop.
          This function only handles server communication and logs one clean line per state change.
    """
    import websockets
    import time as _time

    candidates = provider._get_ws_candidates()

    # --- Phase 1: send command inside a bounded window (fail fast, no open-ended hangs) ---
    try:
        async with asyncio.timeout(MODEL_SWITCH_PHASE1_TIMEOUT):
            async with connect_with_failover(
                candidates, max_size=provider.max_msg_size,
                ping_interval=provider.heartbeat_interval, ping_timeout=provider.ping_timeout,
            ) as ws:
                api_key = provider._load_api_key()
                await ws.send(json.dumps({"type": "auth", "api_key": api_key, "role_type": "client",
                                    "protocol_version": PROTOCOL_VERSION}))

                auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))
                if auth_response.get("type") != "auth_ok":
                    return False, f"Auth failed: {auth_response}"
                _check_protocol(auth_response, "direct switch")

                await ws.send(json.dumps({"type": "model_switch", "model": model_filename}))

                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    if msg.get("type") == "model_switch_response":
                        success = msg.get("success", False)
                        message = msg.get("message", "")
                        return success, message
                except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                    # No fast response - SERVER started the reload and dropped us. Expected path.
                    pass
    except TimeoutError:  # asyncio.timeout expiry (py3.11+)
        logger.info(f"[MODEL SWITCH] Phase 1 window ({MODEL_SWITCH_PHASE1_TIMEOUT}s) expired while waiting for SERVER to accept the command - continuing to poll")
    except NoServerReachable as e:
        return False, f"SERVER unavailable at any configured address ({e})"
    except (ConnectionRefusedError, OSError):
        return False, "SERVER unavailable"
    except Exception as e:
        # Unexpected transport problem before we even got a response.
        logger.error(f"[MODEL SWITCH] Phase 1 failed: {type(e).__name__}: {e}")
        return False, f"Could not send switch command to SERVER ({type(e).__name__})"

    # --- Phase 2: silently poll until server restarts with correct model (bounded wait) ---
    logger.info(f"[MODEL SWITCH] Waiting for Server to reload ({model_filename}, up to {MODEL_SWITCH_MAX_WAIT_SEC}s)")

    start = _time.monotonic()
    last_poll_error = ""

    while True:
        await asyncio.sleep(MODEL_SWITCH_POLL_INTERVAL)

        elapsed = int(_time.monotonic() - start)
        if elapsed >= MODEL_SWITCH_MAX_WAIT_SEC:
            detail = f" Last error: {last_poll_error}." if last_poll_error else ""
            return False, (f"Server did not restart with the requested model within "
                           f"{MODEL_SWITCH_MAX_WAIT_SEC}s.{detail}")

        ok, err, _models = await health_check_direct_websocket(provider)
        if not ok:
            # Expected while the SERVER is down/reloading - keep waiting (no error log).
            last_poll_error = err or "health check failed"
            continue

        # Server is up — verify the correct model loaded.
        # force=True: skip the SERVER's fresh-cache shortcut so this check always
        # reflects what llama.cpp actually reports (immediate post-switch confirmation).
        current_model = await get_current_model_direct_websocket(provider, force=True)
        if current_model and _models_match(current_model, model_filename):
            return True, f"Model switched to {model_filename}"
        last_poll_error = f"server up but wrong model ({current_model})"
