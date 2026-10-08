"""CoolemsClientProvider -- Secure WebSocket pipe to remote coolems_server.

Opens encrypted WebSocket connection, forwards params unchanged, relays chunks back, closes.
No intelligence. No modification. Just secure transport.

Direct mode (Settings UI Connection Mode = "direct", the default):
  Connects directly to the SERVER via wss:// with TLS encryption on LAN.

Web relay mode (Settings UI Connection Mode = "web_relay"):
  Connects ONLY to the web relay address saved in settings.json - that is all the
  CLIENT needs. The home SERVER reaches the same relay itself; no SERVER address is
  used or required by this CLIENT in relay mode.

MODULE SPLIT (2026-10):
  - client_provider/connection_modes.py -> model-switch flag + live connection-mode/address helpers
  - client_provider/ws_connect.py       -> _AccumulatingWS + the fail-closed relay_ws_connect helper
  - client_provider/provider_class.py   -> the CoolemsClientProvider class (thin delegations)
  - ssl_helper.py / direct_mode.py / relay_mode.py / tool_requests.py / control_channel.py (siblings)

The flat CLIENT/app/providers/coolems/client_provider.py file is a re-export facade so
existing imports ('from .client_provider import CoolemsClientProvider, is_model_switching,',
'relay_ws_connect') keep working unchanged.
"""

import logging

# Module-level config imports
from config import get_connection_mode, get_web_relay_address, WEB_RELAY_DEFAULT_PORT
from .. import _parse_web_relay_address

logger = logging.getLogger("COOLEMS.Provider.CoolemsClient")

# Global flag to suppress spurious connection errors during model reload
_model_switch_in_progress = False

def is_model_switching() -> bool:

    """Return True while a model switch task is running."""

    return _model_switch_in_progress





def _set_model_switching(value: bool) -> None:

    """Set the global model-switch-in-progress flag.



    Called from agent.py router to suppress spurious connection errors

    during server reload phases.

    """

    global _model_switch_in_progress

    _model_switch_in_progress = value






def _relay_mode() -> bool:
    """(2026-09-23) LIVE connection-mode check: 'web_relay' selected in settings.json
    (Settings UI, default direct). Read fresh on every call so a mode switch applies to the
    very next connection - no restart needed for the mode itself.
    """
    return get_connection_mode() == "web_relay"

def _live_relay_host_port():
    """(2026-09-23) LIVE relay (host, port) from settings.json for the next connection.

    Re-read on every call so an address change in the Settings UI applies to the very
    next connect. Returns None when no live address is configured - callers fall back
    to the construction-time cached values (keeps a misconfiguration from breaking display).
    """
    live = get_web_relay_address()
    if not live:
        logger.warning(
            "[CLIENT] Web relay mode active but no relay host is saved in settings.json - "
            "open Settings -> Connection Mode and enter the web_server_relay address (host + optional port)."
        )
        return None
    try:
        return _parse_web_relay_address(live, WEB_RELAY_DEFAULT_PORT)
    except (ValueError, TypeError):
        logger.warning(f"[CLIENT] Invalid relay address in settings.json: {live!r} - using cached value")
        return None


def _parse_direct_ws_address(addr, default_port):

    """Parse host:port for direct WebSocket connection from COOLEMS_CLIENT_SERVER_ADDRESS."""

    if addr.startswith("["):

        bracket_end = addr.find("]")

        if bracket_end == -1:

            raise ValueError(f"Invalid IPv6 address: {addr}")

        host = addr[1:bracket_end]

    elif ":" in addr:

        host = addr.rsplit(":", 1)[0]

    else:

        host = addr

    return host, default_port
