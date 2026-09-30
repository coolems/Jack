"""Per-connection active-conversation tracker (stop events) + shared singleton.

Moved verbatim from ws_client_handler.py on 2026-09-07 (lines 47-118).
Each direct WS connection owns its entries via composite (conn_id, conv_id) keys.
"""

import asyncio

from .log import logger

class _ActiveConversations:
    """Tracker for active conversations with stop events, OWNED per connection.

    Each active chat request gets an asyncio.Event that, when set, tells the
    streaming layer to abort generation immediately. Prevents orphaned responses
    and wasted GPU cycles when clients disconnect or click stop.

    FIX (2026-08-19) - COMPOSITE KEY (conn_id, conv_id):
      The old key was conv_id alone, which caused two failure modes:
        1. SELF-COLLISION: a second in-flight request with the same conv_id
           replaced the first one's event in the dict; "stop"/disconnect then set
           the NEW event while the first relay still held the OLD one -> the first
           generation could no longer be aborted (orphaned, burned GPU).
        2. CROSS-CLIENT: two connections using the same conv_id shared one entry;
           A's disconnect stopped B's generation, and any client could send "stop"
           for a conv_id it did not own.
      Keys are now (conn_id, conv_id): each connection owns its entries exclusively.
    """

    @staticmethod
    def _key(conn_id: str, conv_id: str) -> tuple[str, str]:
        return (conn_id or "", conv_id)

    def __init__(self):
        self._events: dict[tuple[str, str], asyncio.Event] = {}  # (conn_id, conv_id) -> Event

    def register(self, conn_id: str, conv_id: str) -> asyncio.Event:
        """Register a new conversation for tracking. Returns the stop event.

        Ownership is per-connection: registering under the same (conn_id, conv_id)
        replaces that connection's own previous entry; other connections are never
        touched by this call.
        """
        ev = asyncio.Event()
        self._events[self._key(conn_id, conv_id)] = ev
        return ev

    def unregister_and_stop(self, conn_id: str, conv_id: str) -> bool:
        """Remove tracking and set stop event if still active (owner-scoped).

        Only affects the entry owned by *conn_id* -- a client can never stop or
        clobber another connection's conversation (FIX 2026-08-19).

        Returns True if an active entry was found and stopped, False otherwise.
        """
        ev = self._events.pop(self._key(conn_id, conv_id), None)
        if ev is not None:
            if not ev.is_set():
                ev.set()
            return True
        return False

    def stop_all_for_client(self, client_convs: set[tuple[str, str]]):
        """Stop all conversations owned by a disconnected client.

        *client_convs* holds composite (conn_id, conv_id) keys -- the disconnect
        cleanup can therefore only ever touch THIS connection's entries.
        """
        if not client_convs:
            return
        stopped = 0
        for key in list(client_convs):
            ev = self._events.pop(key, None)
            if ev is not None and not ev.is_set():
                ev.set()
                stopped += 1
        logger.info(f"[SERVER] Stopped {stopped} active conversation(s) on client disconnect")


    
# Module-level singleton — shared across all WS connections
_active_convs = _ActiveConversations()
