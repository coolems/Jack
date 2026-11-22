"""CoolemsClientProvider -- Secure WebSocket pipe to remote coolems_server.

Opens encrypted WebSocket connection, forwards params unchanged, relays chunks back, closes.
No intelligence. No modification. Just secure transport.

Direct mode (Settings UI Connection Mode = "direct", the default):
  Connects directly to the SERVER via wss:// with TLS encryption on LAN.

Web relay mode (Settings UI Connection Mode = "web_relay"):
  Connects ONLY to the web relay address saved in settings.json - that is all the
  CLIENT needs. The home SERVER reaches the same relay itself; no SERVER address is
  used or required by this CLIENT in relay mode.

  - client_provider/connection_modes.py -> model-switch flag + live connection-mode/address helpers
  - client_provider/ws_connect.py       -> _AccumulatingWS + the fail-closed relay_ws_connect helper
  - client_provider/provider_class.py   -> the CoolemsClientProvider class (thin delegations)
  - ssl_helper.py / direct_mode.py / relay_mode.py / tool_requests.py / control_channel.py (siblings)

This package __init__ IS the import surface: 'from .client_provider import
CoolemsClientProvider, is_model_switching, relay_ws_connect' resolves here.
"""

from .connection_modes import (  # noqa: F401
    _begin_model_switch,
    _end_model_switch,
    _live_relay_host_port,
    _model_switch_in_progress,
    _parse_direct_ws_address,
    _relay_mode,
    is_model_switching,
)
from .provider_class import CoolemsClientProvider  # noqa: F401
from .ws_connect import _AccumulatingWS, relay_ws_connect  # noqa: F401