"""Public entry point: handle one direct WS client connection.

Moved verbatim from ws_client_handler.py on 2026-09-07 (lines 121-175).
This function keeps exactly one job: authenticate, then run the message loop;
disconnect cleanup (stop events + conn_tracker) happens in its finally-block.
"""

from ..connection_enforcement import conn_tracker
from .active_conversations import _active_convs
from .log import logger
from .ws_auth import _authenticate_direct_client
from .ws_loop import _run_authenticated_loop

async def handle_direct_ws_client(ws, server_provider):
    """Handle one direct WebSocket client connection with TLS encryption.

    Requires authentication as first message: {"type":"auth","api_key":"..."}
    Rejects unauthenticated clients immediately. Enforces profile-based permissions.
    
    CONNECTION ENFORCEMENT (Fix #2):
        - Checks max_connections limit per API key during auth
        - Rate-limits chat requests using token-bucket algorithm per API key
    
    On disconnect, sets stop events for all active conversations belonging to this client
    so the server aborts generation on llama.cpp side (no orphaned responses).
    """
    import websockets

    peer = ws.remote_address if hasattr(ws, 'remote_address') else "unknown"
    logger.info(f"[SERVER] Direct WS client connected from {peer} (TLS encrypted)")

    # Track which conversations this client owns (for cleanup on disconnect).
    # FIX (2026-08-19): composite (conn_id, conv_id) keys -- see _ActiveConversations.
    client_convs: set[tuple[str, str]] = set()
    
    # Track connection ID for enforcement cleanup (Fix #2)
    conn_id: str | None = None
    authenticated_api_key: str | None = None

    # --- Auth handshake: first message MUST be auth ---
    result = await _authenticate_direct_client(ws, server_provider, peer)
    if result is None:
        return  # Client rejected or disconnected during auth

    user_info, authenticated_api_key, conn_id = result

    # Guard against partial auth failure - defensive check (Fix #2-d)
    if not user_info or not authenticated_api_key:
        logger.warning(f"[SERVER] Direct WS client {peer} - auth returned incomplete data, disconnecting")
        return

    
    # --- Main message loop (authenticated) ---
    # (2026-08-20 split, review item #10): the per-message dispatch moved into
    # _run_authenticated_loop() so this function keeps exactly one job: auth + lifecycle.
    # client_convs (declared above, before auth) is mutated in place by the loop and
    # used by the finally-block for disconnect cleanup -- do NOT rebind it here.
    try:
        await _run_authenticated_loop(
            ws, server_provider, user_info, conn_id, authenticated_api_key, client_convs
        )
    finally:
        # Client disconnected — abort ALL active conversations for this client
        _active_convs.stop_all_for_client(client_convs)

        # (2026-09-08 multi-chat) also cancel this connection's QUEUED requests:
        # their stop events are already set by stop_all_for_client, but queued entries
        # must be removed from the backlog so workers don't pop dead work.
        if conn_id is not None:
            try:
                from app.server_queue import get_queue

                queue = get_queue()
                if queue is not None:
                    for key in client_convs:
                        await queue.cancel_owner(f"{key[0]}|{key[1]}")
            except Exception as e:  # pragma: no cover - cleanup must never raise
                logger.warning(f"[SERVER] Queue cancel on disconnect failed for {peer}: {e}")

        # Clean up connection tracking (Fix #2)
        if conn_id is not None:
            await conn_tracker.unregister(conn_id)
