"""WebSocket database operations.

All SQLite interactions for the WebSocket chat handler live here: conversation creation
and lookup, message persistence and the per-chat working-root sync. Every write goes
through app.db_manager, so WAL mode, busy timeout and foreign keys are uniform.

Per-workspace contract: each save refreshes conversations.working_root for ITS OWN row
from that row's stored value, never from the folder currently open in the UI - so
background workspaces keep their own folder while another chat is being viewed.
_ensure_conversation_exists guarantees the row exists first, and a sync that updates 0
rows logs an ERROR instead of disappearing silently.

save_user_message / save_assistant_message RETURN the new message id so a turn's
cycle-history row (app/websocket/turn_ops.save_turn_log) can link to the answer it
describes; NULL message_id is reserved for turns that never produced an answer.
"""

import uuid
import json
import logging
from typing import List, Dict, Optional

from app.db_manager import get_db_connection, close_db_connection

logger = logging.getLogger("COOLEMS.WebSocket.DB")


def _ensure_conversation_exists(cursor, conv_id: str) -> None:
    """Make sure the conversation row exists before we try to update it.

    a missing conversations row made every
    UPDATE ... WHERE id = ? a silent no-op (rowcount 0), so renaming the
    working root in the UI was never written to the database until the AI
    happened to be used again. Creating the row here guarantees the sync
    below always has something to update - it is committed together with
    the message insert by the caller, or rolled back with it on error.
    """
    cursor.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,))
    if cursor.fetchone() is None:
        logger.warning(
            "Conversation %s missing in DB - creating row so working_root can be persisted",
            conv_id,
        )
        cursor.execute(
            "INSERT INTO conversations (id, title) VALUES (?, 'New Workspace')",
            (conv_id,),
        )


def _sync_conversation_working_root(cursor, conv_id: str) -> None:
    """Refresh this conversation's own working root onto its row (idempotent).
    PER-WORKSPACE CONTRACT: the value written is resolved from THIS conversation's OWN
    stored value - never from the in-memory ACTIVE cache.
    Now the sync is pure bookkeeping: it writes the row's own value back (no-op), or pins a
    fresh/NULL row to the project-root fallback so every workspace owns its row from first
    use. The UI chip / conversation switch are the only writers that ever CHANGE a row's
    folder.
    """
    try:
        from app.utils.common import resolve_working_root_for_conv
        cursor.execute(
            "UPDATE conversations SET working_root = ? WHERE id = ?",
            (resolve_working_root_for_conv(conv_id), conv_id),
        )
        if cursor.rowcount == 0:
            logger.error(
                "working_root sync wrote 0 rows for conversation %s - "
                "conversation row missing, rename NOT persisted", conv_id,
            )
    except Exception as e:
        # Never break message saving because of the extra bookkeeping column.
        logger.warning(f"Could not sync working_root for conversation {conv_id}: {e}")




def save_user_message(
    conn_path: str,
    conv_id: str,
    content: str,
    media_urls: Optional[List[str]] = None,
    file_contents: Optional[List[Dict]] = None,
    auto_title: bool = True,
    is_steer: bool = False,
):
    """Save a user message to the database.

    Args:
        conn_path: SQLite database path.
        conv_id: conversation ID.
        content: message text.
        media_urls: optional list of media file URLs.
        file_contents: optional list of file content dicts.
        auto_title: when True and the workspace title is "New Workspace", rename it.
        is_steer: True for a LIVE STEERING message typed while a turn was running.
    """
    msg_id = str(uuid.uuid4())
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # FIX (2026-08-30): guarantee the conversation row exists so the
        # working_root sync below can never be a silent no-op UPDATE.
        _ensure_conversation_exists(cursor, conv_id)
        cursor.execute(
            "INSERT INTO messages (id, conversation_id, role, content, media_urls, file_contents, is_steer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                conv_id,
                "user",
                content,
                json.dumps(media_urls) if media_urls else None,
                json.dumps(file_contents) if file_contents else None,
                1 if is_steer else 0,
            ),
        )
        cursor.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conv_id,),
        )
        _sync_conversation_working_root(cursor, conv_id)

        # Auto-title if the default placeholder ("New Workspace"; legacy rows may still say "New Chat")
        if auto_title:
            cursor.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,))
            row = cursor.fetchone()
            if row and row[0] in ("New Workspace", "New Chat"):  # (2026-10-02) workspace rename: keep auto-titling legacy rows too
                new_title = content.strip()[:30]
                if len(content.strip()) > 30:
                    new_title += "..."
                cursor.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (new_title, conv_id),
                )

        conn.commit()
        return msg_id
    finally:
        close_db_connection(conn)


def save_assistant_message(
    conn_path: str,
    conv_id: str,
    content: str,
):
    """Save an assistant (AI) response to the database."""
    msg_id = str(uuid.uuid4())
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # FIX (2026-08-30): guarantee the conversation row exists so the
        # working_root sync below can never be a silent no-op UPDATE.
        _ensure_conversation_exists(cursor, conv_id)
        cursor.execute(
            "INSERT INTO messages (id, conversation_id, role, content, media_urls, file_contents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, conv_id, "assistant", content, None, None),
        )
        cursor.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conv_id,),
        )
        _sync_conversation_working_root(cursor, conv_id)
        conn.commit()
        # The id lets the turn's cycle-history row reference THIS answer
        # (turn_log.message_id); NULL is reserved for turns with no answer.
        return msg_id
    finally:
        close_db_connection(conn)


def save_search_result(
    conn_path: str,
    conv_id: str,
    user_msg: str,
    assistant_msg: str,
):
    """Save both user and assistant messages for a /search command result.
    Used by search_handler.py.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # FIX (2026-08-30): guarantee the conversation row exists so the
        # working_root sync below can never be a silent no-op UPDATE.
        _ensure_conversation_exists(cursor, conv_id)
        cursor.execute(
            "INSERT INTO messages (id, conversation_id, role, content, media_urls, file_contents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), conv_id, "user", user_msg, None, None),
        )
        cursor.execute(
            "INSERT INTO messages (id, conversation_id, role, content, media_urls, file_contents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), conv_id, "assistant", assistant_msg, None, None),
        )
        cursor.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conv_id,),
        )
        _sync_conversation_working_root(cursor, conv_id)
        conn.commit()
    finally:
        close_db_connection(conn)


def save_partial_response(
    conn_path: str,
    conv_id: str,
    partial_response: str,
):
    """Save a partial response when the user disconnects mid-generation.
    Appends '[Generation stopped by user]' marker.
    """
    content = partial_response + "\n\n_[Generation stopped by user]_"
    msg_id = str(uuid.uuid4())
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # FIX (2026-08-30): guarantee the conversation row exists so the
        # working_root sync below can never be a silent no-op UPDATE.
        _ensure_conversation_exists(cursor, conv_id)
        cursor.execute(
            "INSERT INTO messages (id, conversation_id, role, content, media_urls, file_contents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, conv_id, "assistant", content, None, None),
        )
        cursor.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conv_id,),
        )
        _sync_conversation_working_root(cursor, conv_id)
        conn.commit()
    finally:
        close_db_connection(conn)
