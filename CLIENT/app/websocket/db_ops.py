"""
WebSocket database operations.

All SQLite interactions for the WebSocket chat handler are centralized here.
Uses the centralized db_manager for safe pooled connections with WAL mode.

UPDATED (2025-06-25):
    - Refactored to use centralized db_manager instead of raw sqlite3.connect()

UPDATED (2026-08-29):
    - Per-chat working root: every message save also refreshes
      conversations.working_root for its OWN conversation row,
      so clicking a chat later can restore exactly that folder.

FIXED (2026-10-09):
    - The sync used to stamp the ACTIVE UI working root onto whatever row was
      saved, corrupting background workspaces' rows while you viewed another
      one ("working_root stops changing after the first switch"). It now
      resolves each conversation's OWN stored value (per-workspace DB contract).

FIXED (2026-08-30):
    - UI-only working-root renames were silently lost: the sync UPDATE had no
      rowcount check and ran only as part of message saves, so when no message
      save happened afterwards (or the conversation row was missing) nothing
      was ever written to the database. Now every save guarantees the
      conversation row exists (_ensure_conversation_exists) and a 0-row sync
      UPDATE logs an ERROR instead of disappearing.
"""

import uuid
import json
import logging
from typing import List, Dict, Optional

from app.db_manager import get_db_connection, close_db_connection

logger = logging.getLogger("COOLEMS.WebSocket.DB")


def _ensure_conversation_exists(cursor, conv_id: str) -> None:
    """Make sure the conversation row exists before we try to update it.

    FIX (2026-08-30): a missing conversations row made every
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

    PER-WORKSPACE CONTRACT (2026-10-09): the value written is resolved from THIS
    conversation's OWN stored value - never from the in-memory ACTIVE cache. The
    old code stamped get_working_root() (the folder open in the UI) onto whatever
    row was being saved, so a BACKGROUND workspace finishing its turn while you
    viewed another one silently overwrote its own row with the other workspace's
    path - after that, switching back "restored" the wrong folder and the working
    root appeared to stop changing.

    Now the sync is pure bookkeeping: it writes the row's own value back (no-op),
    or pins a fresh/NULL row to the project-root fallback so every workspace owns
    its row from first use. The UI chip / conversation switch are the only writers
    that ever CHANGE a row's folder.

    FIX (2026-08-30, kept): verify rowcount after the UPDATE - a 0-row update means
    the conversation row was missing and nothing was written; _ensure_conversation_exists()
    prevents it and the failure logs loudly instead of disappearing silently.
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
):
    """
    Save a user message to the database.

    Args:
        conn_path: Path to the SQLite database (kept for API compatibility).
        conv_id: Conversation ID.
        content: Message text.
        media_urls: Optional list of media file URLs.
        file_contents: Optional list of file content dicts.
        auto_title: If True and workspace title is "New Workspace" (or legacy "New Chat"), update it.
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
            (
                str(uuid.uuid4()),
                conv_id,
                "user",
                content,
                json.dumps(media_urls) if media_urls else None,
                json.dumps(file_contents) if file_contents else None,
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
    finally:
        close_db_connection(conn)


def save_assistant_message(
    conn_path: str,
    conv_id: str,
    content: str,
):
    """Save an assistant (AI) response to the database."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # FIX (2026-08-30): guarantee the conversation row exists so the
        # working_root sync below can never be a silent no-op UPDATE.
        _ensure_conversation_exists(cursor, conv_id)
        cursor.execute(
            "INSERT INTO messages (id, conversation_id, role, content, media_urls, file_contents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), conv_id, "assistant", content, None, None),
        )
        cursor.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conv_id,),
        )
        _sync_conversation_working_root(cursor, conv_id)
        conn.commit()
    finally:
        close_db_connection(conn)


def save_search_result(
    conn_path: str,
    conv_id: str,
    user_msg: str,
    assistant_msg: str,
):
    """
    Save both user and assistant messages for a /search command result.
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
    """
    Save a partial response when the user disconnects mid-generation.
    Appends '[Generation stopped by user]' marker.
    """
    content = partial_response + "\n\n_[Generation stopped by user]_"
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # FIX (2026-08-30): guarantee the conversation row exists so the
        # working_root sync below can never be a silent no-op UPDATE.
        _ensure_conversation_exists(cursor, conv_id)
        cursor.execute(
            "INSERT INTO messages (id, conversation_id, role, content, media_urls, file_contents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), conv_id, "assistant", content, None, None),
        )
        cursor.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conv_id,),
        )
        _sync_conversation_working_root(cursor, conv_id)
        conn.commit()
    finally:
        close_db_connection(conn)
