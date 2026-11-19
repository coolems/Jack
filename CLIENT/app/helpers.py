"""Small request helpers shared by CLIENT routers.

Client IP extraction for HTTP requests and WebSockets, used to attribute conversations
per machine (conversations.ip_address = the workspace owner).

X-Forwarded-For is honoured ONLY when client_config.TRUST_PROXY_HEADERS is True. The header is
client-controlled and the extracted value is what conversation ownership is keyed on, so
trusting it by default would let any caller claim another machine's identity. Without a
reverse proxy the socket address is the only trustworthy source.
"""

from fastapi import Request, WebSocket

from client_config import TRUST_PROXY_HEADERS


def get_client_ip(request: Request) -> str:
    """Get the client IP for conversation ownership.

    Default: the real socket address. X-Forwarded-For is read only behind an
    operator-controlled reverse proxy (client_config.TRUST_PROXY_HEADERS), where the socket
    address belongs to the proxy and the header is the only usable identity.

    """
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()

    client = request.client
    if client:
        return client.host

    return "unknown"


def get_ws_client_ip(websocket: WebSocket) -> str:
    """Get client IP from WebSocket."""
    if websocket.client:
        return websocket.client.host
    return "unknown"
