"""Chat relay — forwards chat requests to local LLM and streams response back over WebSocket.

2026-09-08 multi-chat (Phase 2) split: the transport-independent generation core lives in
:func:`run_chat_core` and is shared by BOTH entry points below AND by the queue worker pool
(`app/server_queue/worker_pool.py`). The two legacy functions are now thin shims that keep
the old call signatures working unchanged.

Frame routing contract (unchanged from before the split):
  * llama streaming chunks arrive via ``sink.send_text(json)`` — the sink is either a raw
    _WSAdapter (shim path, exactly as before) or a FrameSink (worker path).
  * final response / token_stats / done / error frames are sent through the same object.
"""

import json
import logging
from typing import Tuple, Type, Optional

# Server is the single source of truth for the default generation temperature:
# when a client request omits 'temperature', fall back to config LLAMA_SERVER_TEMP
# (the same value llama-server itself is started with) instead of a hardcoded 0.7.
from config import LLAMA_SERVER_TEMP

# Typed, EXPECTED user-facing condition: image sent to a model without mmproj.
# Caught specifically below so it logs ONE clean line instead of a full traceback.
from ..llama.vision_support import VisionNotAvailableError

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")


def _is_expected_disconnect(exc: BaseException) -> bool:
    """L6 fix: decide whether a send failure is an *expected* client disconnect.

    The old implementation substring-matched "1000"/"closed"/"ok" in str(e), which
    is brittle across websockets versions (message wording changes between releases
    and could match unrelated errors). Detection now uses the exception TYPE and,
    for the `websockets` library, the RFC6455 close code carried on the exception:

      - ConnectionClosedOK            → normal closure (code 1000) — always expected
      - ConnectionClosedError         → abnormal closure; only "expected" if it
                                        carries a normal-policy close code (1000-1003,
                                        1005, 1007-1011, 3000) i.e. the peer closed
                                        cleanly rather than the transport failing
      - OSError / generic Exception   → NOT an expected disconnect (real error)

    Unknown exception types default to False (log as a real send failure).
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
    """Lightweight wrapper that presents a uniform send_text() interface over any WebSocket.

    Used by both direct-client and relay-mode so chat_stream() can call
    websocket.send_text(...) without caring about the underlying transport.
    
    Gracefully handles closed connections — logs at DEBUG level for expected disconnects.
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


async def _safe_ws_send(ws, msg_dict: dict):
    """Send a JSON message to WebSocket, silently ignoring closed connections."""
    try:
        await ws.send(json.dumps(msg_dict))
    except Exception:
        # Connection already closed — this is normal when client disconnects
        pass


async def _sink_send_frame(sink, msg_dict: dict):
    """Send one JSON frame through a sink object (send_text interface).

    Used by run_chat_core for the final response/token_stats/done/error frames.
    Works with both the legacy _WSAdapter and the FrameSink adapters from
    app/server_queue/sink.py; never raises on a closed transport.
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
    """Keep only the tools the caller's profile is allowed to use (S1 fix behaviour)."""
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
    """Estimate prompt size INCLUDING the tool schema (client-side estimates exclude it -
    that drift is what let prompts reach 130k+ tokens against a 131k window).

    FIXED (2026-08): include tool_calls JSON in the estimate - content-only sums
    counted assistant tool-call messages as 0 tokens and this log line was blind
    to the fastest-growing part of agentic history.
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
                        temperature: float = LLAMA_SERVER_TEMP, enable_thinking: bool = False,
                        conv_id: str = "", tools=None, stop_event=None):
    """Run one chat turn against *provider* and stream every frame through *sink*.

    *sink* needs ``send_text(str)`` + ``is_closed`` — satisfied by both _WSAdapter (legacy
    shim path) and the FrameSink adapters in app/server_queue/sink.py. Returns True when a
    final response was delivered, False when the client vanished mid-stream (nothing sent).

    This function NEVER raises for transport problems: a disconnected client is a normal,
    logged condition; generation errors are turned into an "error" frame.
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
            stop_event=stop_event  # abort llama.cpp generation on client stop/disconnect
        )

        # Check if client disconnected during streaming — skip sending response
        if sink.is_closed:
            logger.info(f"[SERVER] Client disconnected during stream for conv={conv_id} — discarding response")
            return False

        logger.info(f"[SERVER] chat_stream returned. response_len={len(response_text) if response_text else 0}")

        await _sink_send_frame(sink, {"type": "response", "content": response_text})

        if token_stats:
            await _sink_send_frame(sink, {"type": "token_stats", **token_stats.to_dict()})

        await _sink_send_frame(sink, {"type": "done"})
        return True

    except VisionNotAvailableError as e:
        # Expected user-facing condition (image sent to a model without mmproj), NOT a
        # server fault — one clean log line, no traceback. The client already sees the
        # same message in its "Vision Not Available" card; error_type lets it render
        # that dedicated card instead of a generic provider error.
        logger.warning(f"[SERVER] Vision unavailable for conv={conv_id}: {e}")
        await _sink_send_frame(sink, {"type": "error", "message": str(e), "error_type": e.error_type})

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
    """Forward chat request to local LLM and relay response back over Direct WebSocket.

    Filters tools by user profile permissions before relaying.
    Gracefully handles client disconnection without logging errors.

    SERVER-SIDE STOP PROPAGATION:
        Extracts _server_stop_event from msg (injected by direct_ws.ws_loop) and passes it
        to local.chat_stream(). This ensures llama.cpp generation is aborted immediately
        when the client disconnects, instead of running to completion with no one listening.

    2026-09-08 multi-chat: body reduced to a shim over run_chat_core() — behaviour identical.
    """
    model = msg.get("model", "")
    messages = msg.get("messages", [])
    temperature = msg.get("temperature", LLAMA_SERVER_TEMP)
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
    """Forward chat request to local LLM and relay response back over WebSocket (relay mode).
    (2026-09-01 S1 fix) *user_info* is the RELAYED CLIENT's resolved profile.
    It drives tool-schema filtering and - together with _RelayTargetAdapter -
    routes every outgoing frame back to exactly that client over the shared
    brain<->relay socket. None (legacy call) = no extra filtering.

    SERVER-SIDE STOP PROPAGATION (2026-08-20): mirrors the direct path -- extracts
    _server_stop_event from msg (injected by ws_brain_relay, keyed per relayed client)
    and passes it to local.chat_stream() so llama.cpp generation aborts when a client
    behind the relay sends 'stop' or disconnects.

    2026-09-08 multi-chat: body reduced to a shim over run_chat_core() — behaviour identical.
    """
    # Extract server-side stop event (injected by ws_brain_relay per relayed client)
    server_stop_event = msg.pop("_server_stop_event", None)

    model = msg.get("model", "")
    messages = msg.get("messages", [])
    temperature = msg.get("temperature", LLAMA_SERVER_TEMP)
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
