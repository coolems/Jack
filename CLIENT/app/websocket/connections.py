"""
WebSocket Connection Registry & Broadcast.

Tracks all active UI WebSocket connections and provides a broadcast
mechanism to push status messages (model switch progress, etc.) to ALL clients.

FIXED (2026-01-04): Multi-chat interference bug cleanup.
- Connection tracking is per (client_ip, conv_id) for proper multi-tab support.
"""

import asyncio
import logging
from typing import Set, Any, Dict, Tuple

logger = logging.getLogger("COOLEMS")

# Global set of active WebSocket connections (kept for broadcast)
_active_connections: Set[Any] = set()

# Track connections per (client_ip, conv_id) tuple
# Maps client_ip -> set of (conv_id, ws) tuples
_client_conversations: Dict[str, Set[Tuple[str, Any]]] = {}


def register_connection(ws: Any, client_ip: str = "", conv_id: str = "") -> None:
    """Register a new WebSocket connection.
    
    Args:
        ws: The WebSocket object.
        client_ip: Client IP address (for multi-tab tracking).
        conv_id: Conversation ID this WS belongs to.
    """
    global _active_connections, _client_conversations
    _active_connections.add(ws)
    
    if client_ip and conv_id:
        _client_conversations.setdefault(client_ip, set()).add((conv_id, ws))


def unregister_connection(ws: Any, client_ip: str = "", conv_id: str = "") -> None:
    """Remove a WebSocket connection (on disconnect).
    
    Args:
        ws: The WebSocket object.
        client_ip: Client IP address.
        conv_id: Conversation ID this WS belonged to.
    """
    global _active_connections, _client_conversations
    _active_connections.discard(ws)
    
    if client_ip and conv_id:
        conv_set = _client_conversations.get(client_ip)
        if conv_set:
            conv_set.discard((conv_id, ws))
            if not conv_set:
                del _client_conversations[client_ip]


async def broadcast(message: dict) -> int:
    """
    Broadcast a JSON message to ALL active WebSocket connections.

    Dead connections are automatically cleaned up from the registry.

    Args:
        message: Dict to send as JSON (must have a 'type' field).

    Returns:
        Number of clients that successfully received the message.
    """
    global _active_connections
    dead = set()
    sent = 0

    for ws in _active_connections:
        try:
            import json
            await ws.send_text(json.dumps(message))
            sent += 1
        except Exception:
            # Connection is dead - mark for removal
            dead.add(ws)

    # Clean up dead connections
    if dead:
        _active_connections -= dead
        logger.debug(f"[BROADCAST] Removed {len(dead)} dead connections. Active: {len(_active_connections)}")

    return sent


async def broadcast_model_switch_status(status: str, message: str, model_name: str = "") -> None:
    """
    Convenience function to broadcast model switch status updates.

    Sends a 'model_switch_status' message that the UI can handle specially.

    Args:
        status: One of 'started', 'reloading', 'waiting', 'success', 'failed'
        message: Human-readable status text
        model_name: Target model filename (optional)
    """
    payload = {
        "type": "model_switch_status",
        "status": status,
        "message": message,
    }
    if model_name:
        payload["model"] = model_name

    count = await broadcast(payload)
    logger.debug(f"[BROADCAST] model_switch_status '{status}' sent to {count} clients")


def get_active_count() -> int:
    """Return number of active WebSocket connections."""
    return len(_active_connections)


def get_client_conversation_count(client_ip: str) -> int:
    """Return total active conversation count for a client IP."""
    conv_set = _client_conversations.get(client_ip, set())
    return len(conv_set)
