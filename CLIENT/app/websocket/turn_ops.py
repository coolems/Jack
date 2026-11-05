"""Cycle-history (turn_log) database operations.

`turn_log` holds one row per agentic turn: the collapsed cycle block the UI renders
(steps, tool calls, consent dialogs, steers, notices, final answer).

THE DELETE CONTRACT (why workspace deletion used to fail):
    turn_log.conversation_id is a CHILD of conversations.id
    (FOREIGN KEY ... REFERENCES conversations(id)) and app/db_manager sets
    PRAGMA foreign_keys=ON on EVERY connection. Deleting the workspace row while
    its cycle rows still reference it raises
    sqlite3.IntegrityError("FOREIGN KEY constraint failed") -> HTTP 500 ->
    "Failed to delete workspace" in the UI. Children must be cleared FIRST, so all
    workspace deletes go through delete_conversation_rows() below.

Same reason for truncation (edit / resend): cycle rows are children of the
CONVERSATION, not of the answer they describe, so truncate_turn_logs() must prune
them too - otherwise a reload rebuilds cycle blocks for answers that are gone.
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from app.db_manager import get_db_connection, close_db_connection
from app.websocket.db_ops import _ensure_conversation_exists
from app.turn_log import parse_payload

logger = logging.getLogger("COOLEMS.TurnOps")


def save_turn_log(conn_path: str, conv_id: str, payload: Dict,
                  message_id: Optional[str] = None) -> bool:
    """Persist one turn's cycle history.

    message_id is NULL for a turn that never produced an answer (stopped / failed),
    so the row is placed by timestamp instead of by message.

    Returns True on success. History is best-effort: a failure must never break a
    finished turn, so this logs and returns False instead of raising.
    """
    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            # FK enforcement: the parent conversation row must exist before the child.
            _ensure_conversation_exists(cursor, conv_id)
            cursor.execute(
                "INSERT INTO turn_log (id, conversation_id, message_id, payload) "
                "VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), conv_id, message_id,
                 json.dumps(payload, ensure_ascii=False)),
            )
            conn.commit()
            return True
        finally:
            close_db_connection(conn)
    except Exception as e:
        logger.warning(f"Could not save turn_log for conversation {conv_id}: {e}")
        return False


def load_turn_logs(conv_id: str) -> List[Dict]:
    """Cycle rows for one workspace, oldest first. Never raises - no rows = []."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, message_id, payload, created_at FROM turn_log "
            "WHERE conversation_id = ? ORDER BY created_at, rowid",
            (conv_id,),
        )
        rows = cursor.fetchall()
    finally:
        close_db_connection(conn)

    out: List[Dict] = []
    for row in rows:
        payload = parse_payload(row[2])
        if payload is None:
            continue   # unreadable row: skip rather than break the reload
        out.append({"id": row[0], "message_id": row[1],
                    "payload": payload, "created_at": row[3]})
    return out


def delete_conversation_rows(cursor, conv_id: str) -> Dict[str, int]:
    """Delete a workspace CHILD-FIRST: turn_log -> messages -> conversations.

    Order is the whole point. With foreign_keys ON, clearing the children before the
    parent is the only sequence that cannot raise IntegrityError, and it keeps the
    delete atomic: the caller commits once, so a workspace goes together with its
    messages and cycle history - no orphan rows left behind.
    """
    counts: Dict[str, int] = {}

    cursor.execute("DELETE FROM turn_log WHERE conversation_id = ?", (conv_id,))
    counts["turn_log"] = cursor.rowcount

    cursor.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
    counts["messages"] = cursor.rowcount

    cursor.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    counts["conversations"] = cursor.rowcount

    return counts


def truncate_turn_logs(cursor, conv_id: str, message_ids: List[str],
                       from_timestamp: Any = None) -> int:
    """Prune cycle rows that belong to answers removed by a truncation.

    Two rules, because a row is tied to the conversation, not to its answer:
      1. rows linked to a deleted message_id go with it;
      2. rows with message_id NULL (stopped / failed turns) are placed by time, so
         anything created at or after the first removed message goes too.
    """
    if not message_ids:
        return 0

    placeholders = ",".join("?" * len(message_ids))
    removed = 0

    cursor.execute(
        f"DELETE FROM turn_log WHERE conversation_id = ? AND message_id IN ({placeholders})",
        [conv_id, *message_ids],
    )
    removed += cursor.rowcount

    if from_timestamp is not None:
        cursor.execute(
            "DELETE FROM turn_log WHERE conversation_id = ? AND message_id IS NULL "
            "AND created_at >= ?",
            (conv_id, from_timestamp),
        )
        removed += cursor.rowcount

    return removed


def prune_orphan_turn_logs(conn) -> int:
    """Clean cycle rows left by an older build that deleted without FK enforcement.

    Runs at startup: those rows are not owned by any workspace but are still returned
    by the history API, so a workspace that no longer exists would keep a history entry.
    """
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM turn_log WHERE conversation_id NOT IN (SELECT id FROM conversations)"
    )
    removed = cursor.rowcount
    conn.commit()
    if removed:
        logger.info(f"Pruned {removed} orphan turn_log row(s)")
    return removed
