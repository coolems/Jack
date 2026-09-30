"""
Conversation endpoints - CRUD operations for conversations and messages.

UPDATED (2025-07-12):
    - Added 'color' field support for conversations (8 color palette)
    - Extended list endpoint with sort_by and sort_order query params
    - Sort options: created (by creation time), color, name (by title)
    - Order options: asc (ascending), desc (descending)

UPDATED (2025-06-25):
    - Refactored to use centralized db_manager for safe pooled connections with WAL mode
"""
import json
import uuid
from typing import Dict
from fastapi import APIRouter, Request, HTTPException, Query

from app.helpers import get_client_ip
from app.db_manager import get_db_connection, close_db_connection
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
                (conv_id, "New Chat", model_name, current_ip)
            )
            conn.commit()
        finally:
            close_db_connection(conn)

        logger.info(f"Created conversation {conv_id} for client {current_ip}")
        return {"id": conv_id, "title": "New Chat"}

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
                SELECT id, title, model, agent_mode, created_at, ip_address, color
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
                    "title": row[1] or "New Chat",
                    "model": row[2],
                    "agent_mode": bool(row[3]),
                    "created_at": row[4],
                    "owner_ip": row[5],
                    "color": row[6] if len(row) > 6 and row[6] else None,
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
                "SELECT role, content, media_urls, file_contents, timestamp FROM messages WHERE conversation_id = ? ORDER BY timestamp",
                (conv_id,)
            )
            rows = cursor.fetchall()
            cursor.execute("SELECT system_prompt, agent_mode, working_root FROM conversations WHERE id = ?", (conv_id,))
            conv_info_full = cursor.fetchone()
        finally:
            close_db_connection(conn)

        messages = []
        for row in rows:
            messages.append({
                "role": row[0],
                "content": row[1],
                "media_urls": json.loads(row[2]) if row[2] else [],
                "file_contents": json.loads(row[3]) if row[3] else [],
                "timestamp": row[4]
            })

        return {
            "messages": messages,
            "system_prompt": conv_info_full[0] if conv_info_full else f"You are {agent.get_name()}.",
            "agent_mode": bool(conv_info_full[1]) if conv_info_full else False,
            # Per-chat working root (2026-08-29): additive field. NULL for legacy chats -> UI keeps current folder.
            "working_root": conv_info_full[2] if conv_info_full and len(conv_info_full) > 2 else None,
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
        try:
            from app.chat_bus import get_chat_bus
            bus = get_chat_bus()
            if bus is not None:
                ch = bus.channels.get(conv_id)
                if ch is not None:
                    status = ch.status.value
                    queue_position = int(getattr(ch, "queue_position", 0) or 0)
        except Exception as e:  # pragma: no cover - bus must never break the list/status UI
            logger.debug(f"Conversation status lookup failed for {conv_id}: {e}")

        return {"status": status, "queue_position": queue_position}

    @router.post("/{conv_id}/update")
    async def update_conversation(conv_id: str, data: Dict, request: Request):
        """
        Update conversation properties.

        Accepts: title, system_prompt, agent_mode, color
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
            if "agent_mode" in data:
                cursor.execute("UPDATE conversations SET agent_mode = ? WHERE id = ?", (data["agent_mode"], conv_id))
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
            cursor = conn.cursor()
            cursor.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            cursor.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            conn.commit()
        finally:
            close_db_connection(conn)

        logger.info(f"Client {current_ip} deleted conversation: {conv_id}")
        return {"status": "deleted"}

    @router.delete("/{conv_id}/messages/truncate")
    async def truncate_messages(conv_id: str, data: Dict, request: Request):
        current_ip = get_client_ip(request)
        message_index = data.get("message_index", 0)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT id FROM messages WHERE conversation_id = ? ORDER BY timestamp",
                (conv_id,)
            )
            rows = cursor.fetchall()

            deleted_count = 0
            if message_index < len(rows):
                ids_to_delete = [row[0] for row in rows[message_index:]]
                deleted_count = len(ids_to_delete)
                placeholders = ",".join("?" * deleted_count)
                cursor.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids_to_delete)
                conn.commit()
                logger.info(f"Client {current_ip} truncated {deleted_count} messages from conversation {conv_id}")
        finally:
            close_db_connection(conn)
        return {"status": "truncated", "deleted_count": deleted_count, "remaining_count": message_index}

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
