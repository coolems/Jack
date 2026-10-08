"""WebSocket relay brain mode - connects to external web relay as a brain server.

Handles reconnection logic, auth with the relay, and message routing from relay clients
to the local LLM backend. The implementation is split across this package:

    connection.py      TLS setup (fail-closed), gate key, connect + reconnect loop
    request_handler.py per-relayed-client chat-request pipeline (auth -> rate limit ->
                       model authorization -> stop propagation -> queue enqueue)
    relay_handlers.py  _RelayTargetAdapter, _relay_send and the model_switch / tools /
                       tool_code message handlers (keyed on the RELAYED client's profile)

The flat app/providers/coolems/ws_brain_relay.py file is a thin re-export facade so
existing imports ('from .ws_brain_relay import connect_to_web_relay', '_RelayTargetAdapter')
keep working unchanged.
"""

from .connection import connect_to_web_relay  # noqa: F401
from .request_handler import _handle_relay_request  # noqa: F401
from .relay_handlers import (  # noqa: F401
    _RelayTargetAdapter,
    _handle_relay_model_switch,
    _handle_relay_tool_code_request,
    _handle_relay_tools_request,
    _relay_send,
    _resolve_relay_client_user_info,
)