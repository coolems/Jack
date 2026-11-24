"""WebSocket database operations.

All SQLite interactions for the WebSocket chat handler live here: conversation creation
and lookup and message persistence. Working-folder settings are not written by message saves.

Both message helpers return the inserted ID. The user-message helper can also
notify its caller when it changes the workspace title.
"""

import uuid
import json
import logging
from typing import Callable, List, Dict, Optional

from app.db_manager import get_db_connection, close_db_connection

logger = logging.getLogger("COOLEMS.WebSocket.DB")


def _ensure_conversation_exists(cursor, conv_id: str) -> None:
    """Create the workspace row if needed before inserting its messages."""
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


def save_user_message(
    conn_path: str,
    conv_id: str,
    content: str,
    media_urls: Optional[List[str]] = None,
    file_contents: Optional[List[Dict]] = None,
    auto_title: bool = True,
    is_steer: bool = False,
    on_title_changed: Callable[[str], None] | None = None,
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
        on_title_changed: Optional callback for a committed title change.
    """
    msg_id = str(uuid.uuid4())
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
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

        # Auto-title if the default placeholder ("New Workspace"; legacy rows may still say "New Chat")
        new_title = None
        if auto_title:
            cursor.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,))
            row = cursor.fetchone()
            text = content.strip()
            if text and row and row[0] in ("New Workspace", "New Chat"):
                candidate = text[:30] + ("..." if len(text) > 30 else "")
                if candidate != row[0]:
                    new_title = candidate
            if new_title is not None:
                cursor.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (new_title, conv_id),
                )

        conn.commit()
        if new_title is not None and on_title_changed is not None:
            on_title_changed(new_title)
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
        conn.commit()
    finally:
        close_db_connection(conn)
