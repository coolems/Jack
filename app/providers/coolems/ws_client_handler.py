"""Compatibility shim for the direct-WS client handler (split 2026-09-07).

The former single-file ws_client_handler.py (825 lines) was split into the
direct_ws/ package -- one small module per concern, each moved verbatim:

    log.py                    shared logger (same name as before)
    active_conversations.py   _ActiveConversations + _active_convs singleton
    model_filters.py          _tool_name_of / _normalize_model_entry / _filter_models_by_user
    ws_auth.py                _authenticate_direct_client (handshake, protocol, limits)
    ws_loop.py                _run_authenticated_loop (per-frame router)
    model_switch_handler.py   _handle_model_switch_direct
    tools_request_handler.py  _load_profile_blocked_libs + _handle_tools_request
    tool_code_handler.py      _handle_tool_code_request
    ocr_handler.py            _handle_ocr_request
    ws_entry.py               handle_direct_ws_client (public entry point)

Every name the old module exposed is re-exported here so all existing import
paths keep working unchanged:

    from .ws_client_handler import handle_direct_ws_client          # server_provider
    from .ws_client_handler import _ActiveConversations, ...        # ws_brain_relay
    from .ws_client_handler import _tool_name_of                    # chat_relay

The _active_convs singleton is the SAME object as direct_ws._active_convs --
stop events registered through either path are shared.
"""

from .direct_ws import (  # noqa: F401
    logger,
    _ActiveConversations,
    _active_convs,
    handle_direct_ws_client,
    _authenticate_direct_client,
    _run_authenticated_loop,
    _tool_name_of,
    _normalize_model_entry,
    _filter_models_by_user,
    _handle_model_switch_direct,
    _load_profile_blocked_libs,
    _handle_tools_request,
    _handle_tool_code_request,
    _handle_ocr_request,
)
