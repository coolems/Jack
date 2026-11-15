"""
Conversation endpoints - CRUD operations for conversations and messages.

UPDATED (2025-07-12):
    - Added 'color' field support for conversations (8 color palette)
    - Extended list endpoint with sort_by and sort_order query params
    - Sort options: created (by creation time), color, name (by title)
    - Order options: asc (ascending), desc (descending)

UPDATED (2025-06-25):
    - Refactored to use centralized db_manager for safe pooled connections with WAL mode

FIXED (2026-12-04): workspace deletion failed with "FOREIGN KEY constraint failed"
    (HTTP 500 -> "Failed to delete workspace"). turn_log rows are CHILDREN of the
    conversation, so DELETE must clear them first - both delete and truncate now go
    through app.websocket.turn_ops.
"""
import json
import uuid
from typing import Dict
from fastapi import APIRouter, Request, HTTPException, Query

from app.helpers import get_client_ip
from app.db_manager import get_db_connection, close_db_connection
from app.websocket.turn_ops import delete_conversation_rows, truncate_turn_logs, load_turn_logs
import logging

logger = logging.getLogger("COOLEMS")

# Predefined color palette for conversations (8 colors)
VALID_COLORS = [
    "red", "orange", "yellow", "green", "blue", "purple", "pink", "gray"
]

def create_conversations_router(db_path: str, model_name: str, agent) -> APIRouter:
    """Factory function that returns a configured conversations router."""
    router = APIRouter(prefix="/api/conversations", tags=["conversations"])

    @router.post("")
    async def create_conversation(request: Request):
        current_ip = get_client_ip(request)

        conv_id = str(uuid.uuid4())
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO conversations (id, title, model, ip_address) VALUES (?, ?, ?, ?)",
                (conv_id, "New Workspace", model_name, current_ip)
            )
            conn.commit()
        finally:
            close_db_connection(conn)

        logger.info(f"Created conversation {conv_id} for client {current_ip}")
        return {"id": conv_id, "title": "New Workspace"}

    @router.get("")
    async def list_conversations(
        request: Request,
        sort_by: str = Query("created", regex="^(created|color|name)$"),
        sort_order: str = Query("desc", regex="^(asc|desc)$"),
    ):
        """
        List conversations for the current client with optional sorting.

        - sort_by: 'created' (default), 'color', or 'name'
        - sort_order: 'asc' or 'desc' (default 'desc')
        """
        current_ip = get_client_ip(request)

        # Map sort_by to SQL column
        sort_map = {
            "created": "created_at",
            "color": "color",
            "name": "title",
        }
        order_dir = "ASC" if sort_order == "asc" else "DESC"
        order_col = sort_map.get(sort_by, "created_at")

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT id, title, model, created_at, ip_address, color
                FROM conversations
                WHERE ip_address = ?
                ORDER BY {order_col} {order_dir}
            """, (current_ip,))
            rows = cursor.fetchall()
        finally:
            close_db_connection(conn)

        return {
            "sort_by": sort_by,
            "sort_order": sort_order,
            "conversations": [
                {
                    "id": row[0],
                    "title": row[1] or "New Workspace",
                    "model": row[2],
                    "created_at": row[3],
                    "owner_ip": row[4],
                    "color": row[5] if len(row) > 5 and row[5] else None,
                }
                for row in rows
            ]
        }

    @router.get("/{conv_id}/messages")
    async def get_messages(conv_id: str, request: Request):
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            cursor.execute("SELECT ip_address FROM conversations WHERE id = ?", (conv_id,))
            conv_info = cursor.fetchone()

            if not conv_info:
                raise HTTPException(status_code=404, detail="Conversation not found")

            logger.info(f"Client {current_ip} viewing conversation {conv_id}")

            cursor.execute(
                "SELECT id, role, content, media_urls, file_contents, timestamp, is_steer FROM messages "
                "WHERE conversation_id = ? ORDER BY timestamp, rowid",
                (conv_id,)
            )
            rows = cursor.fetchall()
            cursor.execute("SELECT system_prompt, working_root FROM conversations WHERE id = ?", (conv_id,))
            conv_info_full = cursor.fetchone()
        finally:
            close_db_connection(conn)

        messages = []
        for row in rows:
            messages.append({
                # (2026-11-08) the id anchors a cycle block: turn_log.message_id points at
                # the answer that turn produced, so the UI rebuilds each block right above it.
                "id": row[0],
                "role": row[1],
                "content": row[2],
                "media_urls": json.loads(row[3]) if row[3] else [],
                "file_contents": json.loads(row[4]) if row[4] else [],
                "timestamp": row[5],
                # (2026-11-08, user request) a steering message is LOOP WORK, not a chat
                # message: the UI skips these rows and shows them inside the turn's
                # loop-history block instead.
                "is_steer": bool(row[6]) if len(row) > 6 else False,
            })

        return {
            "messages": messages,
            "system_prompt": conv_info_full[0] if conv_info_full else f"You are {agent.get_name()}.",
            # Per-chat working root (2026-08-29): additive field. NULL for legacy chats -> UI keeps current folder.
            "working_root": conv_info_full[1] if conv_info_full and len(conv_info_full) > 1 else None,
            # Cycle history (2026-11-08): one row per agentic turn, already normalized
            # by turn_ops.load_turn_logs -> the UI rebuilds collapsed cycle blocks from it.
            "turn_logs": load_turn_logs(conv_id),
            "owner_ip": conv_info[0]
        }

    @router.get("/{conv_id}/status")
    async def get_conversation_status(conv_id: str, request: Request):
        """Live session state of one chat (2026-09-08 multi-chat UI fix).

        Returns {"status": "idle"|"queued"|"generating"|..., "queue_position": int}
        straight from the ChatBus channel, so a chat switch can restore the
        generation indicator and send/stop button color even when no frames are
        currently flowing (e.g. the turn finished while the user was elsewhere).
        """
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT ip_address FROM conversations WHERE id = ?", (conv_id,))
            conv_info = cursor.fetchone()
        finally:
            close_db_connection(conn)

        if not conv_info:
            raise HTTPException(status_code=404, detail="Conversation not found")

        status = "idle"
        queue_position = 0
        agent_mode = False
        try:
            from app.chat_bus import get_chat_bus
            bus = get_chat_bus()
            if bus is not None:
                ch = bus.channels.get(conv_id)
                if ch is not None:
                    status = ch.status.value
                    queue_position = int(getattr(ch, "queue_position", 0) or 0)
                    # (2026-10-23 live steering) whether the live turn accepts steering
                    agent_mode = bool(getattr(ch, "agent_mode", False))
        except Exception as e:  # pragma: no cover - bus must never break the list/status UI
            logger.debug(f"Conversation status lookup failed for {conv_id}: {e}")

        return {"status": status, "queue_position": queue_position, "agent_mode": agent_mode}
    
    @router.post("/{conv_id}/update")
    async def update_conversation(conv_id: str, data: Dict, request: Request):
        """
        Update conversation properties.

        Accepts: title, system_prompt, color
        - color: one of VALID_COLORS or None to clear
        """
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            if "title" in data:
                cursor.execute("UPDATE conversations SET title = ? WHERE id = ?", (data["title"], conv_id))
            if "system_prompt" in data:
                cursor.execute("UPDATE conversations SET system_prompt = ? WHERE id = ?", (data["system_prompt"], conv_id))
            if "color" in data:
                # Accept None/null to clear color, or one of VALID_COLORS
                new_color = data["color"]
                if new_color is not None and new_color not in VALID_COLORS:
                    new_color = None  # invalid color - clear
                cursor.execute("UPDATE conversations SET color = ? WHERE id = ?", (new_color, conv_id))

            conn.commit()
        finally:
            close_db_connection(conn)

        logger.info(f"Client {current_ip} updated conversation {conv_id}")
        return {"status": "updated"}

    @router.delete("/{conv_id}")
    async def delete_conversation(conv_id: str, request: Request):
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            # CHILD-FIRST: cycle history and messages reference the conversation row,
            # and foreign keys are ON on every connection - clearing the parent first
            # raises IntegrityError and the workspace can never be deleted.
            counts = delete_conversation_rows(conn.cursor(), conv_id)
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to delete conversation {conv_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete workspace")
        finally:
            close_db_connection(conn)

        logger.info(
            f"Client {current_ip} deleted conversation: {conv_id} "
            f"(messages={counts['messages']}, turn_logs={counts['turn_log']})"
        )
        return {
            "status": "deleted",
            "deleted_messages": counts["messages"],
            "deleted_turn_logs": counts["turn_log"],
        }

    @router.delete("/{conv_id}/messages/truncate")
    async def truncate_messages(conv_id: str, data: Dict, request: Request):
        current_ip = get_client_ip(request)
        message_index = data.get("message_index", 0)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT id, timestamp FROM messages WHERE conversation_id = ? "
                "ORDER BY timestamp, rowid",
                (conv_id,)
            )
            rows = cursor.fetchall()

            deleted_count = 0
            pruned_turn_logs = 0
            if message_index < len(rows):
                ids_to_delete = [row[0] for row in rows[message_index:]]
                deleted_count = len(ids_to_delete)
                placeholders = ",".join("?" * deleted_count)
                cursor.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids_to_delete)
                # Cycle rows belong to the CONVERSATION, not to the answer they describe:
                # without this prune, a reload rebuilds cycle blocks for answers that are gone.
                pruned_turn_logs = truncate_turn_logs(
                    cursor, conv_id, ids_to_delete, rows[message_index][1]
                )
                conn.commit()
                logger.info(
                    f"Client {current_ip} truncated {deleted_count} messages from "
                    f"conversation {conv_id} (turn_logs={pruned_turn_logs})"
                )
        finally:
            close_db_connection(conn)
        return {"status": "truncated", "deleted_count": deleted_count,
                "remaining_count": message_index, "deleted_turn_logs": pruned_turn_logs}

    @router.post("/{conv_id}/reset-state")
    async def reset_conversation_state(conv_id: str, request: Request):
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            cursor.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (conv_id,)
            )
            conn.commit()
        finally:
            close_db_connection(conn)

        logger.info(f"Client {current_ip} reset conversation state for {conv_id}")
        return {"status": "reset"}

    return router
