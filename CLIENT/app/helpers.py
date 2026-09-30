"""Small request helpers shared by CLIENT routers.

Provides client IP extraction for HTTP requests and WebSockets
(X-Forwarded-For aware), used to attribute conversations per machine.
"""

from fastapi import Request, WebSocket


def get_client_ip(request: Request) -> str:
    """Get real client IP from request."""
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
