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

import json
from contextlib import asynccontextmanager

class _AccumulatingWS:

    """Helper websocket mock that accumulates content for non-streaming mode."""



    def __init__(self) -> None:

        """Collect content/thinking chunks into a list for chat_non_streaming."""

        self.texts = []



    async def send_text(self, data):

        """Accumulate 'content'/'thinking' frames; ignore all other types."""

        msg = json.loads(data)

        if msg.get("type") in ("content", "thinking"):

            self.texts.append(msg.get("content", ""))


@asynccontextmanager
async def relay_ws_connect(provider, **connect_kwargs):
    """Open ONE trusted-TLS WebSocket to the web relay (fail-closed).

    (2026-09-23 v4) The client->relay link is internet-facing. This helper builds the
    SSL context via provider._build_ssl_context() and REFUSES to connect when it comes
    back None (no CA verification / no usable cert pin configured) - there is no silent
    CERT_NONE fallback for relay connections anymore. Used by every relay_mode method.
    """
    import websockets

    ws_url, ssl_ctx = provider._get_ws_url_and_ssl()

    if ssl_ctx is None:
        raise ConnectionError(
            "Web relay connection refused - no trusted TLS configured. Set WEB_RELAY_VERIFY_SSL_CERTS=True "
            "(public CA, e.g. a public certificate authority) or WEB_RELAY_CERT_PIN_FINGERPRINT in CLIENT config."
        )

    async with websockets.connect(ws_url, ssl=ssl_ctx, **connect_kwargs) as ws:
        yield ws
