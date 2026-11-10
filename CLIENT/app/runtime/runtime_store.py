"""runtime_store — durable ledger for subagents + background jobs (client side, 2026-12).

The live registries (app.runtime.children / app.runtime.jobs) are PROCESS-LOCAL: a
restart cannot resume a child's conversation or a job's output stream. What IS durable is
the ledger — identity, owner, status, terminal detail — which is what makes these
questions answerable after a restart:

  * "what did this workspace delegate, and how did each end?"
  * "did this job finish, or did the process die mid-run?"
  * a workspace delete must clear the ledger rows BEFORE the conversation row.

A row whose settlement never arrived keeps finished_at NULL = 'incomplete'. We never
invent a finish time: an unfinished job is reported as unfinished.

Every function is best-effort (logs and returns False) — persistence must never break a
settlement, exactly like turn_log.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from app.db_manager import get_db_connection, close_db_connection
from app.websocket.db_ops import _ensure_conversation_exists

logger = logging.getLogger("COOLEMS.Runtime.Store")


def save_runtime_row(*, conv_id: str, kind: str, status: str, label: str = "",
                     mode: str = "", depth: int = 0, detail: str = "",
                     result: str = "", row_id: str = "",
                     finished: bool = False) -> bool:
    """Insert or update one ledger row (idempotent on the registry id).

    row_id is the registry's own id ("child-xxxx" / "bash-xxxx"), so the same id updates
    its row instead of duplicating it. finished=True stamps finished_at; a settlement that
    never arrives leaves it NULL.
    """
    row_id = row_id or f"{kind}-{uuid.uuid4().hex[:8]}"
    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            _ensure_conversation_exists(cursor, conv_id)
            cursor.execute(
                "INSERT INTO runtime_state (id, conversation_id, kind, status, label, mode, "
                "depth, detail, result, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, label=excluded.label, "
                "detail=excluded.detail, result=excluded.result",
                (row_id, conv_id, kind, status, label[:200], mode, int(depth),
                 detail[:500], result[:2000]),
            )
            if finished:
                cursor.execute(
                    "UPDATE runtime_state SET finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row_id,),
                )
            conn.commit()
            return True
        finally:
            close_db_connection(conn)
    except Exception as e:
        logger.warning(f"Could not save runtime_state row {row_id} for conv {conv_id}: {e}")
        return False


def load_runtime_rows(conv_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Ledger rows for one workspace, oldest first. Never raises - no rows = []."""
    conn = get_db_connection()
    try:
        sql = ("SELECT id, kind, status, label, mode, depth, detail, result, "
               "started_at, finished_at FROM runtime_state WHERE conversation_id = ?")
        args: List[Any] = [conv_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY started_at, rowid"
        rows = conn.execute(sql, args).fetchall()
    finally:
        close_db_connection(conn)

    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append({
            "id": row[0], "kind": row[1], "status": row[2], "label": row[3] or "",
            "mode": row[4] or "", "depth": int(row[5] or 0), "detail": row[6] or "",
            "result": row[7] or "", "started_at": row[8], "finished_at": row[9],
            # finished_at NULL = the process died before settlement: incomplete, not failed.
            "incomplete": row[9] is None and row[2] in ("running", "stopping"),
        })
    return out


def delete_runtime_rows(cursor, conv_id: str) -> int:
    """Clear a workspace's ledger rows (CHILD-FIRST, before the conversation row).

    Called from app.websocket.turn_ops.delete_conversation_rows(): runtime_state is a
    child of conversations with foreign_keys ON, so deleting the parent first raises
    IntegrityError and the whole delete fails as HTTP 500.
    """
    cursor.execute("DELETE FROM runtime_state WHERE conversation_id = ?", (conv_id,))
    return cursor.rowcount


def prune_orphan_runtime_rows(conn) -> int:
    """Clean ledger rows left by an older build that deleted without FK enforcement."""
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM runtime_state WHERE conversation_id NOT IN (SELECT id FROM conversations)"
    )
    removed = cursor.rowcount
    conn.commit()
    if removed:
        logger.info(f"Pruned {removed} orphan runtime_state row(s)")
    return removed
