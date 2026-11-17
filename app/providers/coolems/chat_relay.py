"""Chat relay - send a chat request to the local LLM and stream the response back over
WebSocket.

run_chat_core() is the transport-independent core, shared by the two entry points below
and by the queue worker pool (app/server_queue/worker_pool.py); relay_chat_websocket /
relay_chat_websocket_direct are shims over it. Streaming chunks and the final
response/token_stats/done/error frames all go through the same sink (send_text).
"""

import json
import logging
from typing import Tuple, Type, Optional, Any

# Server is the single source of truth for the default generation temperature:
# when a client request omits 'temperature', fall back to config LLAMA_SERVER_TEMP
# (the same value llama-server itself is started with) instead of a hardcoded 0.7.
from config import LLAMA_SERVER_TEMP

import httpx

# Typed, EXPECTED user-facing condition: image sent to a model without mmproj.
# Caught specifically below so it logs ONE clean line instead of a full traceback.
from ..llama.vision_support import VisionNotAvailableError

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")


def _is_expected_disconnect(exc: BaseException) -> bool:
    """Whether a send failure is an expected client disconnect.

    ConnectionClosedOK -> expected; ConnectionClosedError -> expected only with a
    normal-policy close code; OSError and anything else -> not expected (defaults False).
    """
    exc_type_name = type(exc).__name__
    if exc_type_name == "ConnectionClosedOK":
        return True
    if exc_type_name in ("ConnectionClosedError", "ConnectionClosed"):
        code = getattr(exc, "code", None)
        if code is not None:
            # Normal closure codes (RFC6455 §7.4.1/§7.4.2 + library sentinel 3000)
            return code in (1000, 1001, 1002, 1003, 1005, 1007, 1008, 1009, 1010, 1011, 3000)
        return False
    return False


class _WSAdapter:
    """Uniform send_text() wrapper over any WebSocket, so chat_stream() does not care about
    the transport. Logs expected disconnects at DEBUG instead of raising.
    """

    def __init__(
        self,
        ws,
        exc_types: Tuple[Type[BaseException], ...] = (Exception,),
        error_tag: str = "WS",
    ):
        self._ws = ws
        self._closed = False
        self._exc_types = exc_types
        self._error_tag = error_tag

    async def send_text(self, text: str) -> None:
        if self._closed:
            return
        try:
            await self._ws.send(text)
        except self._exc_types as e:
            # ConnectionClosedOK (code 1000) is expected when client disconnects — log at DEBUG, not ERROR
            # L6 fix: type/close-code based detection instead of substring matching
            if _is_expected_disconnect(e):
                logger.debug(f"[{self._error_tag}] Client disconnected (expected): {type(e).__name__}: {e}")
            else:
                logger.warning(f"[{self._error_tag}] Send failed: {e}")
            self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed


def _make_direct_ws_adapter(ws) -> _WSAdapter:
    """Wrapper for a direct (client <-> server) WebSocket — broad exception catch."""
    return _WSAdapter(ws, exc_types=(Exception,), error_tag="SERVER->CLIENT")


def _make_relay_ws_adapter(ws) -> _WSAdapter:
    """Wrapper for a relay (server <-> web-relay) WebSocket — specific exception catch."""
    import websockets.exceptions as ws_exc
    return _WSAdapter(
        ws,
        exc_types=(ws_exc.ConnectionClosed, OSError),
        error_tag="SERVER->RELAY",
    )

async def _sink_send_frame(sink, msg_dict: dict):
    """Send one JSON frame through a sink (send_text interface).

    Used by run_chat_core for the final response/token_stats/done/error frames; never raises
    on a closed transport.
    """
    try:
        await sink.send_text(json.dumps(msg_dict))
    except Exception as e:
        # Connection already closed — this is normal when client disconnects
        logger.debug(f"[SERVER] Final frame not delivered (client gone): {e}")


# ---------------------------------------------------------------------------
# Shared helpers (tool filtering + prompt-size estimate)
# ---------------------------------------------------------------------------

def _extract_tool_names(tools: list) -> list:
    """Collect tool function names from a schema list (OpenAI-style or flat)."""
    names = []
    for t in tools:
        if isinstance(t, dict):
            fn = t.get("function", {})
            if isinstance(fn, dict) and "name" in fn:
                names.append(fn["name"])
            elif "name" in t:
                names.append(t["name"])
    return names


def _filter_tools_by_profile(server_provider, user_info, tools):
    """Keep only the tools the caller's profile is allowed to use (behaviour)."""
    if not user_info or not tools:
        return tools
    tool_names = _extract_tool_names(tools)
    filtered_tool_names = server_provider._auth_gateway.get_allowed_tools(user_info, tool_names)

    def _get_tn(t):
        if isinstance(t, dict):
            fn = t.get("function", {})
            return (fn.get("name") if isinstance(fn, dict) else None) or t.get("name")
        return ""

    removed = set(tool_names) - set(filtered_tool_names)
    if removed:
        logger.info(f"[SERVER] Profile role={user_info.get('role')} blocked tools from schema: {removed}")
    return [t for t in tools if _get_tn(t) in set(filtered_tool_names)]


def _estimate_prompt_chars(messages, tools):
    """Prompt-size estimate INCLUDING the tool schema and tool-call JSON.

    A content-only sum counts assistant tool-call messages as 0 tokens, hiding the fastest
    growing part of agentic history and letting prompts overrun the context window.
    """
    est_chars = sum(
        len(str(m.get("content", ""))) + (len(json.dumps(m["tool_calls"])) if m.get("tool_calls") else 0)
        for m in messages if isinstance(m, dict)
    )
    tools_chars = len(json.dumps(tools)) if tools else 0
    return est_chars, tools_chars


# ---------------------------------------------------------------------------
# Transport-independent generation core (shared by shims AND the queue worker pool)
# ---------------------------------------------------------------------------

async def run_chat_core(server_provider, provider, sink, *, model: str, messages: list,
                        temperature: float = LLAMA_SERVER_TEMP, enable_thinking: Any = False,
                        conv_id: str = "", tools=None, stop_event=None,
                        context_window: Optional[int] = None):
    """Run one chat turn against *provider*, streaming every frame through *sink*.

    *sink* needs send_text(str) + is_closed. Returns True when a final response was
    delivered, False when the client vanished mid-stream. Never raises for transport
    problems: a disconnect is logged, generation errors become an "error" frame.
    """
    est_chars, tools_chars = _estimate_prompt_chars(messages, tools)
    logger.info(
        f"[SERVER] Running chat turn: model={model}, msgs={len(messages)}, conv={conv_id}, "
        f"est_prompt_tokens~{(est_chars + tools_chars) // 4} (tool-schema ~{tools_chars // 4})"
    )

    try:
        response_text, token_stats = await provider.chat_stream(
            model=model, messages=messages, websocket=sink,
            temperature=temperature, enable_thinking=enable_thinking,
            conv_id=conv_id, tools=tools,
            stop_event=stop_event,  # abort llama.cpp generation on client stop/disconnect
            context_window=context_window,  # per-node context (2026-10 fleet)
        )

        # Check if client disconnected during streaming — skip sending response
        if sink.is_closed:
            logger.info(f"[SERVER] Client disconnected during stream for conv={conv_id} — discarding response")
            return False

        logger.info(f"[SERVER] chat_stream returned. response_len={len(response_text) if response_text else 0}")

        await _sink_send_frame(sink, {"type": "response", "content": response_text})

        if token_stats:
            # Every request round-trip re-delivers the slim license payload so CLIENT
            # badges stay fresh without any client-side check.
            from app import license_server
            await _sink_send_frame(sink, {"type": "token_stats", **token_stats.to_dict(),
                                          "license": license_server.current_status()})

        await _sink_send_frame(sink, {"type": "done"})
        return True

    except VisionNotAvailableError as e:
        # Expected user-facing condition (image sent to a model without mmproj), NOT a
        # server fault — one clean log line, no traceback. The client already sees the
        # same message in its "Vision Not Available" card; error_type lets it render
        # that dedicated card instead of a generic provider error.
        logger.warning(f"[SERVER] Vision unavailable for conv={conv_id}: {e}")
        await _sink_send_frame(sink, {"type": "error", "message": str(e), "error_type": e.error_type})

    except (ConnectionError, TimeoutError, httpx.HTTPError) as e:
        # Expected user-facing condition: the llama backend is down, unreachable or
        # dropped mid-stream (e.g. the user stopped/restarted llama-server themselves).
        # Log ONE clean line - no traceback. The client already receives a clear,
        # actionable message below.
        logger.warning(f"[SERVER] LLM backend unavailable for conv={conv_id} (expected): {e}")
        await _sink_send_frame(sink, {"type": "error", "message": f"LLM request failed on SERVER: {str(e)[:800]}"})

    except Exception as e:
        # L6 fix: type/close-code based detection instead of substring matching on str(e)
        if _is_expected_disconnect(e):
            logger.info(f"[SERVER] Client disconnected for conv={conv_id} (expected)")
        else:
            logger.error(f"[SERVER] Error running chat turn for conv={conv_id}: {e}", exc_info=True)
            # Send the REAL error to the client - previously it only saw a bare
            # "Server connection lost" with no idea what happened on the server.
            await _sink_send_frame(sink, {"type": "error", "message": f"LLM request failed on SERVER: {str(e)[:800]}"})

    return False


# ---------------------------------------------------------------------------
# Legacy entry points — thin shims over run_chat_core (signatures unchanged)
# ---------------------------------------------------------------------------

async def relay_chat_websocket_direct(server_provider, msg: dict, ws):
    """Forward a chat request to the local LLM and relay the answer over Direct WebSocket.

    Filters tools by the user's profile permissions. _server_stop_event from msg (injected by
    direct_ws.ws_loop) is passed to chat_stream so generation aborts when the client
    disconnects. Thin shim over run_chat_core().
    """
    model = msg.get("model", "")
    messages = msg.get("messages", [])
    temperature = msg.get("temperature", LLAMA_SERVER_TEMP)
    # Verbatim on purpose: a bool or a canonical level
    # ("none"/"low"/"medium"/"high"). bool() here would erase the user's level.
    enable_thinking = msg.get("enable_thinking", False)
    conv_id = msg.get("conv_id", "")
    tools = _filter_tools_by_profile(server_provider, getattr(ws, "_user_info", None),
                                     msg.get("tools") if msg.get("tools") else None)

    # Extract server-side stop event (injected by direct_ws.ws_loop before relay)
    server_stop_event = msg.pop("_server_stop_event", None)

    sink = _make_direct_ws_adapter(ws)
    local = server_provider._get_local_provider()
    await run_chat_core(
        server_provider, local, sink,
        model=model, messages=messages, temperature=temperature,
        enable_thinking=enable_thinking, conv_id=conv_id, tools=tools,
        stop_event=server_stop_event,
    )


async def relay_chat_websocket(server_provider, msg: dict, ws, user_info=None):
    """Forward a chat request to the local LLM and relay the answer over the web relay.

    *user_info* is the relayed client's profile: it drives tool-schema filtering and routes
    every outgoing frame back to that client over the shared brain<->relay socket. None means
    no extra filtering. _server_stop_event (keyed per relayed client) aborts generation on
    'stop' or disconnect. Thin shim over run_chat_core().
    """
    # Extract server-side stop event (injected by ws_brain_relay per relayed client)
    server_stop_event = msg.pop("_server_stop_event", None)

    model = msg.get("model", "")
    messages = msg.get("messages", [])
    temperature = msg.get("temperature", LLAMA_SERVER_TEMP)
    # Verbatim: bool or canonical level, never coerced.
    enable_thinking = msg.get("enable_thinking", False)
    conv_id = msg.get("conv_id", "")
    tools = _filter_tools_by_profile(server_provider, user_info,
                                     msg.get("tools") if msg.get("tools") else None)

    # (2026-09-01 S1 fix / Phase 3) route EVERY outgoing frame back to the requesting
    # relayed client: the adapter tags each JSON frame with target_client.
    from .ws_brain_relay import _RelayTargetAdapter
    sink = _make_relay_ws_adapter(_RelayTargetAdapter(ws, msg.get("_relay_from") or ""))

    local = server_provider._get_local_provider()
    await run_chat_core(
        server_provider, local, sink,
        model=model, messages=messages, temperature=temperature,
        enable_thinking=enable_thinking, conv_id=conv_id, tools=tools,
        stop_event=server_stop_event,
    )
