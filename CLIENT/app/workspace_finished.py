"""Per-workspace "finished & unread" flag.

One boolean on the conversations row (`unread_finished`) is the single source of truth
for "this workspace finished a task and the user has not looked at it yet":

  * the generation runner SETS it the moment a turn ends (done / error), so a background
    workspace is flagged even if nobody was watching;
  * the conversations list RETURNS it, so a hard restart / reload still glows the sidebar
    row for a workspace that finished while the client was closed;
  * `POST /api/conversations/{id}/seen` CLEARS it — and only a real user click reaches that
    route — so the glow resets exactly when the user opens the workspace.

Both helpers follow the never-raise style of turn_ops.save_turn_log(): a failure here must
never break the turn that is finishing, so they log and move on.
"""

import logging

from app.db_manager import get_db_connection, close_db_connection

logger = logging.getLogger("COOLEMS.WorkspaceFinished")


def mark_workspace_finished(conv_id: str) -> None:
    """Set the finished-unread flag for a workspace. Never raises."""
    if not conv_id:
        return
    try:
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE conversations SET unread_finished = 1 WHERE id = ?",
                (conv_id,),
            )
            conn.commit()
        finally:
            close_db_connection(conn)
    except Exception as e:
        logger.warning(f"Could not mark workspace finished for {conv_id}: {e}")


def clear_workspace_finished(conv_id: str) -> None:
    """Reset the finished-unread flag (the user opened the workspace). Never raises."""
    if not conv_id:
        return
    try:
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE conversations SET unread_finished = 0 WHERE id = ?",
                (conv_id,),
            )
            conn.commit()
        finally:
            close_db_connection(conn)
    except Exception as e:
        logger.warning(f"Could not clear workspace finished for {conv_id}: {e}")
