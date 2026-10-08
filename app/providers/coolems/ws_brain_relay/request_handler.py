"""Per-relayed-client chat-request pipeline for the brain relay link.

_handle_relay_request() runs every 'request' frame through, in order:
  1. resolve the RELAYED CLIENT's profile (fail-closed when unresolvable),
  2. per-client rate limiting (bucket keyed 'relay:<client_id>'),
  3. per-client model authorization (exact folder-name match via auth_gateway),
  4. owner-scoped stop-event registration (SERVER-side stop propagation),
  5. enqueue into the shared server queue with a RelayTargetSink (multi-chat).

Kept out of connection.py so the reconnect/auth loop stays readable; it is called from
the main message loop in connect_to_web_relay() when msg_type == 'request'. Every denial
sends its error frame and returns - exactly what the original inline branch did with a
'continue' back to the message loop.
"""

import logging

from app.auth_gateway.model_resolver import is_model_allowed
from ..connection_enforcement import rate_limiter
from config import LLAMA_SERVER_TEMP, PROFILE_DEFAULT_RATE_LIMIT
from .connection import _relay_convs  # shared stop-event tracker for the brain<->relay link
from .relay_handlers import _relay_send, _resolve_relay_client_user_info

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")


async def _handle_relay_request(ws, server_provider, msg, client_relay_convs):
    # (2026-10-07 split) *client_relay_convs* is the per-link set owned by
    # connect_to_web_relay() - passed explicitly now that this function no longer
    # closes over it (it was a local in the monolith's inline branch).
    # (2026-09-01 S1 fix) resolve the RELAYED CLIENT's profile FIRST. Every
    # authorization decision below (rate limit, model access, tool filtering)
    # uses THIS client's permissions -- never the brain socket's own admin
    # profile. Unresolvable client -> fail-closed denial frame.
    _client_user_info = await _resolve_relay_client_user_info(ws, msg, server_provider)
    if not _client_user_info:
        await _relay_send(ws, {
            "type": "error",
            "message": (f"Request denied for relayed client "
                        f"{(msg.get('_relay_from') or 'unknown')!r} (unresolvable profile)."),
        }, msg.get("_relay_from") or "")
        return
    # --- Rate limit enforcement PER RELAYED CLIENT (S1 fix) ---
    # Old code keyed the bucket on the BRAIN's admin key, so every client
    # behind one relay shared a single bucket. Key 'relay:<client_id>' is
    # unique per client and can never collide with a real API key.
    _rl_key = f"relay:{msg.get('_relay_from') or 'unknown'}"
    _client_max_rate = _client_user_info.get("max_rate_limit", PROFILE_DEFAULT_RATE_LIMIT)
    if not await rate_limiter.allow_request(_rl_key, _client_max_rate):
        logger.warning(
            f"[SERVER] Brain relay rate limit exceeded for client={_rl_key} "
            f"(limit={_client_max_rate}/s)"
        )
        # Notify the offending client through relay instead of silently dropping (Fix #2-c)
        await _relay_send(ws, {
            "type": "error",
            "message": (f"Rate limit exceeded ({_client_max_rate} requests/sec). "
                        f"Slow down or upgrade your profile."),
        }, msg.get("_relay_from") or "")
        return
    # --- Per-client model authorization (S1 fix) ---
    _req_model = msg.get("model", "")
    local_pre = server_provider._get_local_provider()
    all_models_pre = await local_pre.get_models_list()
    known_folders_pre = {
        m["name"].lower(): m["folder"]
        for m in all_models_pre
        if isinstance(m, dict) and m.get("name") and m.get("folder")
    }
    if not is_model_allowed(_client_user_info, _req_model, known_folders_pre):
        logger.warning(
            f"[SERVER] Chat request DENIED via relay: model '{_req_model}' "
            f"not allowed for client role={_client_user_info.get('role')} (client={msg.get('_relay_from', '?')})"
        )
        await _relay_send(ws, {
            "type": "error",
            "message": f"Model '{_req_model}' is not available for your profile.",
        }, msg.get("_relay_from") or "")
        return
    logger.info(f"[SERVER<-RELAY] Chat request via relay (client={msg.get('_relay_from', '?')}, role={_client_user_info.get('role')})")
    # (2026-08-20) SERVER-SIDE STOP PROPAGATION for the relay path:
    # register an owner-scoped stop event keyed by the RELAY's client id
    # and hand it to the queued request so llama.cpp generation aborts when
    # the client sends 'stop' or disappears behind the relay.
    _relay_client_id = msg.get("_relay_from") or "unknown"
    _conv_id = msg.get("conv_id", "")
    _stop_ev = _relay_convs.register(_relay_client_id, _conv_id)
    client_relay_convs.add((_relay_client_id, _conv_id))
    # (2026-09-08 multi-chat) ENQUEUE instead of inline await: the relay
    # receive loop returns immediately; frames come back over this shared
    # brain socket tagged with target_client via the request's sink.
    from app.server_queue import ChatRequest, RelayTargetSink, get_queue
    _queue = get_queue()
    if _queue is None:
        logger.error("[SERVER] Request queue not initialized - cannot accept relayed chat requests")
        await _relay_send(ws, {
            "type": "error",
            "message": "Server request queue unavailable (internal error). Please retry.",
        }, msg.get("_relay_from") or "")
        return
    from ..chat_relay import _filter_tools_by_profile
    _req = ChatRequest(
        owner_key=f"relay:{_relay_client_id}|{_conv_id}",
        sink=RelayTargetSink(ws, _relay_client_id),
        model=msg.get("model", ""),
        messages=msg.get("messages", []),
        temperature=msg.get("temperature", LLAMA_SERVER_TEMP),
        enable_thinking=bool(msg.get("enable_thinking", False)),
        conv_id=_conv_id,
        tools=_filter_tools_by_profile(server_provider, _client_user_info,
                                       msg.get("tools") if msg.get("tools") else None),
        stop_event=_stop_ev,
    )
    _accepted, _info = await _queue.enqueue(_req)
    if not _accepted:
        logger.warning(f"[SERVER] Relayed chat request rejected (client={_relay_client_id}): {_info}")
        await _relay_send(ws, {
            "type": "error",
            "message": f"Server is busy: {_info}. Please try again shortly.",
        }, msg.get("_relay_from") or "")
        return
    # Let the chat know it is waiting behind other requests (position > 1).
    _pos = int(_info.split()[1]) if _info.startswith("position") else 1
    if _pos > 1:
        await _relay_send(ws, {
            "type": "queue_status",
            "conv_id": _conv_id,
            "position": _pos,
        }, msg.get("_relay_from") or "")
