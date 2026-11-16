"""direct_ws -- direct WebSocket client handler package.

One small module per concern:

    log.py                    shared logger
    active_conversations.py   _ActiveConversations + _active_convs singleton
    model_filters.py          _tool_name_of / _normalize_model_entry / _filter_models_by_user
    ws_auth.py                _authenticate_direct_client (handshake, protocol, limits)
    ws_loop.py                _run_authenticated_loop (per-frame router)
    model_switch_handler.py   _handle_model_switch_direct
    tools_request_handler.py  _load_profile_blocked_libs + _handle_tools_request
    tool_code_handler.py      _handle_tool_code_request
    ocr_handler.py            _handle_ocr_request
    ws_entry.py               handle_direct_ws_client (public entry point)
"""

from .active_conversations import _ActiveConversations, _active_convs
from .log import logger
from .model_filters import _filter_models_by_user, _normalize_model_entry, _tool_name_of
from .model_switch_handler import _handle_model_switch_direct
from .ocr_handler import _handle_ocr_request
from .tool_code_handler import _handle_tool_code_request
from .tools_request_handler import _handle_tools_request, _load_profile_blocked_libs
from .ws_auth import _authenticate_direct_client
from .ws_entry import handle_direct_ws_client
from .ws_loop import _run_authenticated_loop

__all__ = [
    "logger",
    "_ActiveConversations",
    "_active_convs",
    "handle_direct_ws_client",
    "_authenticate_direct_client",
    "_run_authenticated_loop",
    "_tool_name_of",
    "_normalize_model_entry",
    "_filter_models_by_user",
    "_handle_model_switch_direct",
    "_load_profile_blocked_libs",
    "_handle_tools_request",
    "_handle_tool_code_request",
    "_handle_ocr_request",
]
