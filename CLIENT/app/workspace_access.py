"""Workspace ownership guard - ONE place that decides who may touch a workspace.

``conversations.ip_address`` is the workspace owner (see app/routers/conversations.py).
Any route that acts on a workspace looked up BY ID must ask this module first, so the rule
exists once instead of being copied into each router.

Why the file endpoints need it: they resolve the workspace's FOLDER from an id supplied by
the caller (``?conversation_id=`` or the ``X-Conversation-Id`` header). Without this check
any caller that knows another workspace's id can read, write, execute or delete files in
that workspace's folder. The 404 branch matters as much as the 403 one: callers must never
turn an unknown id into "use some other folder" - that is how a typo used to land on the
project root.

Every refusal AND every degraded-trust access is reported through app.security_events, so
the admin can review what the client end tried instead of trusting a single log line.
"""

import logging

from fastapi import HTTPException

from app import security_events
from app.workspace_tree import is_workspace_id

logger = logging.getLogger("COOLEMS")

# Rows with no recorded owner: the schema default and the WebSocket fallback in
# app/websocket/db_ops._ensure_conversation_exists() both leave ip_address 'unknown'.
# Such a workspace has no identity to compare against. It stays REACHABLE - rejecting it
# would hide workspaces the CLIENT itself created over the chat socket - but every such
# access is recorded as 'workspace_unowned', never waved through silently.
NO_OWNER = "unknown"

NOT_FOUND = "Conversation not found"
FOREIGN = "This workspace belongs to another client"


def require_workspace_access(cursor, conv_id: str, current_ip: str, surface: str = "http"):
    """Authorize *conv_id* for *current_ip*; return ``(owner_ip, stored_working_root)``.

    The stored working root travels back with the verdict so callers that need the folder
    pay for ONE query instead of authorizing and then re-reading the same row.

    Raises:
        HTTPException 404: not a workspace id, or no such workspace. Callers must NOT fall
            back to another folder.
        HTTPException 403: the workspace belongs to another client.
    """
    claimed = conv_id.strip() if isinstance(conv_id, str) else ""
    if not claimed or not is_workspace_id(claimed):
        security_events.record(
            "workspace_id_invalid",
            surface=surface, client_ip=current_ip, claimed=conv_id if isinstance(conv_id, str) else repr(conv_id),
        )
        raise HTTPException(status_code=404, detail=NOT_FOUND)

    cursor.execute(
        "SELECT ip_address, working_root FROM conversations WHERE id = ?", (claimed,)
    )
    row = cursor.fetchone()
    if row is None:
        security_events.record(
            "workspace_unknown", surface=surface, client_ip=current_ip, workspace=claimed
        )
        raise HTTPException(status_code=404, detail=NOT_FOUND)

    owner_ip = row[0]
    if owner_ip not in (None, "", NO_OWNER) and owner_ip != current_ip:
        security_events.record(
            "workspace_foreign", surface=surface, client_ip=current_ip, owner_ip=owner_ip, workspace=claimed
        )
        logger.warning(
            f"Client {current_ip} denied access to conversation {claimed} owned by {owner_ip}"
        )
        raise HTTPException(status_code=403, detail=FOREIGN)

    if owner_ip in (None, "") or owner_ip == NO_OWNER:
        # Degraded trust: the row carries no owner to compare against. Allowed by the
        # documented legacy contract, reported so it is never invisible.
        security_events.record(
            "workspace_unowned", surface=surface, client_ip=current_ip, workspace=claimed,
            owner_ip=owner_ip if owner_ip is not None else "NULL",
        )

    return owner_ip, (row[1] if len(row) > 1 else None)


def authorize_workspace(conv_id: str, current_ip: str, surface: str = "http"):
    """Same check for callers that have no cursor open; returns the stored working root."""
    from app.db_manager import get_db_connection, close_db_connection

    conn = get_db_connection()
    try:
        return require_workspace_access(conn.cursor(), conv_id, current_ip, surface=surface)[1]
    finally:
        close_db_connection(conn)
