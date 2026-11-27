"""Conversation endpoints - CRUD operations for conversations and messages.

Sorting: sort_by created|color|name, sort_order asc|desc (color palette: VALID_COLORS).
All connections go through app.db_manager (WAL mode, busy timeout, foreign keys ON).

OWNERSHIP: conversations.ip_address is the workspace owner. GET / lists only the
caller's own rows, and every per-conversation route goes through
_require_conversation_owner - see helpers.get_client_ip for why X-Forwarded-For is not
trusted by default.

DELETE/TRUNCATE: turn_log and messages are CHILDREN of the conversation, so deleting
clears children first (app.websocket.turn_ops) - otherwise the FK constraint turns the
delete into a 500 "Failed to delete workspace".
"""
import json
from typing import Dict
from fastapi import APIRouter, Request, HTTPException, Query

from app.helpers import get_client_ip
from app.workspace_tree import allocate_workspace_id, validate_workspace_parent
from app.workspace_access import require_workspace_access
from app.db_manager import get_db_connection, close_db_connection
from app.websocket.turn_ops import delete_conversation_rows, truncate_turn_logs, load_turn_logs
import logging

logger = logging.getLogger("COOLEMS")

# Predefined color palette for conversations (8 colors)
VALID_COLORS = [
    "red", "orange", "yellow", "green", "blue", "purple", "pink", "gray"
]

# Upper bound for the free-text workspace info note (conversations.workspace_note).
# The note is a single review/memory blob a human reads in a modal, not a document store;
# the cap keeps one oversized paste from bloating the workspace row.
NOTE_MAX_CHARS = 20000


def _parent_working_root(cursor, parent_id: str):
    """The working root a workspace created from *parent_id* inherits.

    Read from the parent's own row inside the creation transaction (the parent id was already
    validated as a workspace of this client by validate_workspace_parent). The value is copied,
    not linked: later changing the parent's folder must not silently re-point existing children.
    NULL / blank stays NULL, so a parent without its own folder hands its child the same
    project-root fallback it resolves to itself.
    """
    row = cursor.execute(
        "SELECT working_root FROM conversations WHERE id = ?", (parent_id,)
    ).fetchone()
    if not row or row[0] is None:
        return None
    value = str(row[0]).strip()
    return value or None


def _require_conversation_owner(cursor, conv_id: str, current_ip: str) -> str:
    """Return the conversation's owner ip, or raise 404 (missing) / 403 (not ours).

    The rule itself lives in app.workspace_access so the file endpoints - which resolve a
    workspace's FOLDER from an id - enforce exactly the same thing.
    """
    return require_workspace_access(cursor, conv_id, current_ip)[0]


def create_conversations_router(db_path: str, model_name: str, agent) -> APIRouter:
    """Factory function that returns a configured conversations router."""
    router = APIRouter(prefix="/api/conversations", tags=["conversations"])

    @router.post("")
    async def create_conversation(request: Request):
        current_ip = get_client_ip(request)

        # (2026-12-04 per-workspace model) the new workspace inherits the model the user
        # currently has in the picker (optional JSON body {"model": "..."}); the global
        # default is only the fallback for legacy callers.
        new_model = model_name
        parent_id = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                parent_id = body.get("parent_id")
                value = body.get("model")
                candidate = value.strip() if isinstance(value, str) else ""
                if candidate:
                    new_model = candidate
        except Exception:
            pass  # no body / not JSON -> default model

        conn = get_db_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            conv_id = allocate_workspace_id(cursor)
            if parent_id is not None:
                validate_workspace_parent(cursor, conv_id, current_ip, parent_id)
            # (2026-11-27, user request) a workspace created FROM another workspace starts in the
            # SAME folder: it inherits the parent's working root instead of the project-root
            # default. Read in this transaction so the copy matches the tree the child was born in.
            inherited_root = _parent_working_root(cursor, parent_id) if parent_id is not None else None
            cursor.execute(
                "INSERT INTO conversations (id, title, model, ip_address, parent_id, working_root) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (conv_id, "New Workspace", new_model, current_ip, parent_id, inherited_root)
            )
            conn.commit()
        finally:
            close_db_connection(conn)

        logger.info(
            f"Created conversation {conv_id} for client {current_ip} (model={new_model})"
            + (f" inheriting working root of {parent_id}: {inherited_root}" if inherited_root else "")
        )
        return {"id": conv_id, "title": "New Workspace", "model": new_model,
                "parent_id": parent_id, "working_root": inherited_root}

    @router.get("")
    async def list_conversations(
        request: Request,
        sort_by: str = Query("created", regex="^(created|color|name)$"),
        sort_order: str = Query("desc", regex="^(asc|desc)$"),
    ):
        """List conversations for the current client with optional sorting.

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
                SELECT id, title, model, created_at, ip_address, color, parent_id, unread_finished
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
                    "color": row[5],
                    "parent_id": row[6],
                    "unread_finished": bool(row[7]),
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

            owner_ip = _require_conversation_owner(cursor, conv_id, current_ip)

            logger.info(f"Client {current_ip} viewing conversation {conv_id}")

            cursor.execute(
                "SELECT id, role, content, media_urls, file_contents, timestamp, is_steer FROM messages "
                "WHERE conversation_id = ? ORDER BY timestamp, rowid",
                (conv_id,)
            )
            rows = cursor.fetchall()
            cursor.execute("SELECT system_prompt, working_root, model FROM conversations WHERE id = ?", (conv_id,))
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
            # (2026-12-04 per-workspace model) the model assigned to THIS workspace - the
            # UI restores the picker from it on switch, so workspaces never drift.
            "model": conv_info_full[2] if conv_info_full and len(conv_info_full) > 2 else None,
            # Cycle history (2026-11-08): one row per agentic turn, already normalized
            # by turn_ops.load_turn_logs -> the UI rebuilds collapsed cycle blocks from it.
            "turn_logs": load_turn_logs(conv_id),
            "owner_ip": owner_ip
        }

    @router.get("/{conv_id}/status")
    async def get_conversation_status(conv_id: str, request: Request):
        """Live session state of one chat.

        Returns {"status": "idle"|"queued"|"generating"|..., "queue_position": int}
        straight from the ChatBus channel, so a chat switch can restore the generation
        indicator and send/stop button color even when no frames are currently flowing
        (e.g. the turn finished while the user was elsewhere).

        """
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            _require_conversation_owner(conn.cursor(), conv_id, current_ip)
        finally:
            close_db_connection(conn)

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

    # (2026-11-28 workspace activity bar) "/live" is ONE path segment, so it can never be
    # confused with the "/{conv_id}/..." routes above/below.
    @router.get("/live")
    async def list_live_conversations(request: Request):
        """Live sessions of THIS client's workspaces right now.

        The sidebar's activity bar has to know that a task is still running even for a
        workspace this browser never opened (page reload, LRU-closed socket) - the client-side
        socket pool only knows what it has actually seen. Reads the ChatBus channel states and
        intersects them with the caller's own workspace ids, so nothing leaks between clients.

        Only queued/generating sessions are reported, plus "needs_attention" for a turn parked
        on an unanswered consent card (the UI cannot re-render that card from the DB).
        """
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM conversations WHERE ip_address = ?", (current_ip,))
            owned = {row[0] for row in cursor.fetchall()}
        finally:
            close_db_connection(conn)

        live = []
        try:
            from app.chat_bus import get_chat_bus
            bus = get_chat_bus()
            if bus is not None and owned:
                for conv_id, channel in list(bus.channels.items()):
                    if conv_id not in owned:
                        continue
                    status = getattr(getattr(channel, "status", None), "value", "idle")
                    if status not in ("queued", "generating"):
                        continue
                    pending = getattr(channel, "exec_approval", None)
                    try:
                        needs_attention = bool(pending.pending_requests())
                    except Exception:  # pragma: no cover - defensive, never break the bar
                        needs_attention = False
                    live.append({
                        "id": conv_id,
                        "status": status,
                        "queue_position": int(getattr(channel, "queue_position", 0) or 0),
                        "needs_attention": needs_attention,
                    })
        except Exception as e:  # pragma: no cover - bus must never break the sidebar bar
            logger.debug(f"Live workspace lookup failed: {e}")

        return {"live": live}

    @router.post("/{conv_id}/seen")
    async def mark_conversation_seen(conv_id: str, request: Request):
        """Reset the finished-unread flag for a workspace (the user opened it).

        This is the ONLY thing that clears the sidebar glow: it runs from an explicit
        click/Enter on the workspace row, so a reload keeps the glow until the user has
        actually looked. Idempotent - a repeated click is harmless. Ownership-guarded like
        every other per-conversation route (404 unknown / 403 not ours).
        """
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            _require_conversation_owner(cursor, conv_id, current_ip)
            cursor.execute(
                "UPDATE conversations SET unread_finished = 0 WHERE id = ?",
                (conv_id,),
            )
            conn.commit()
        finally:
            close_db_connection(conn)

        logger.info(f"Client {current_ip} marked conversation {conv_id} seen")
        return {"status": "seen"}

    # ===== WORKSPACE INFO NOTE (2026-11-27, user request) =====
    # Free-text note a user keeps for ONE workspace ("Edit workspace info" in the sidebar
    # row menu). Stored in conversations.workspace_note, i.e. inside the workspace's own
    # entry: saving it needs no extra record, and deleting the workspace removes it with the
    # row (turn_ops.delete_conversation_rows deletes turn_log -> messages -> conversations).
    @router.get("/{conv_id}/note")
    async def get_workspace_note(conv_id: str, request: Request):
        """Read this workspace's info note together with its CURRENT title.

        The title travels back with the note so the editor can show which workspace it is
        about to write to - a modal that only displayed text could be saved onto the wrong
        row. A never-edited (NULL) note reads as "", so the editor opens blank.
        Ownership-guarded like every other per-workspace route (404 unknown / 403 not ours).
        """
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            _require_conversation_owner(cursor, conv_id, current_ip)
            row = cursor.execute(
                "SELECT title, workspace_note FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
        finally:
            close_db_connection(conn)

        return {
            "id": conv_id,
            "title": (row[0] if row else None) or "New Workspace",
            "note": (row[1] if row else None) or "",
        }

    @router.post("/{conv_id}/note")
    async def save_workspace_note(conv_id: str, data: Dict, request: Request):
        """Overwrite this workspace's info note with the whole submitted text.

        An empty string clears the note (the column goes back to ''). The text is limited to
        NOTE_MAX_CHARS and must be a string - anything else is refused BEFORE the write, so a
        malformed body can never blank a note the user still has.
        """
        current_ip = get_client_ip(request)
        note = data.get("note")
        if not isinstance(note, str):
            raise HTTPException(status_code=422, detail="note must be a string")
        if len(note) > NOTE_MAX_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"Workspace info is limited to {NOTE_MAX_CHARS} characters",
            )

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            _require_conversation_owner(cursor, conv_id, current_ip)
            cursor.execute(
                "UPDATE conversations SET workspace_note = ? WHERE id = ?", (note, conv_id)
            )
            conn.commit()
        finally:
            close_db_connection(conn)

        logger.info(
            f"Client {current_ip} saved workspace info for {conv_id} ({len(note)} chars)"
        )
        return {"status": "saved", "length": len(note)}

    @router.post("/{conv_id}/update")
    async def update_conversation(conv_id: str, data: Dict, request: Request):
        """Update conversation properties.

        Accepts: title, system_prompt, color, model, parent_id
        - color: one of VALID_COLORS or None to clear
        - model: (2026-12-04 per-workspace model) the model assigned to THIS workspace;
          the UI persists the picker choice here so a workspace switch restores it.
        """
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            if "parent_id" in data:
                conn.execute("BEGIN IMMEDIATE")
            owner_ip = _require_conversation_owner(cursor, conv_id, current_ip)

            if "parent_id" in data:
                parent_id = data["parent_id"]
                # Unowned legacy workspaces remain reachable by ID, not as members
                # of a client's private tree. They may still be detached to Root.
                if parent_id is not None and owner_ip != current_ip:
                    raise HTTPException(403, "Only owned workspaces can be moved under a parent")
                validate_workspace_parent(cursor, conv_id, current_ip, parent_id)
                cursor.execute("UPDATE conversations SET parent_id = ? WHERE id = ?", (parent_id, conv_id))
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
            if "model" in data:
                # (2026-12-04 per-workspace model) non-empty string only - an empty value
                # must never wipe the workspace's assignment.
                value = data["model"]
                new_model = value.strip() if isinstance(value, str) else ""
                if new_model:
                    cursor.execute("UPDATE conversations SET model = ? WHERE id = ?", (new_model, conv_id))

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
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            _require_conversation_owner(cursor, conv_id, current_ip)
            if cursor.execute("SELECT 1 FROM conversations WHERE parent_id = ? LIMIT 1", (conv_id,)).fetchone():
                raise HTTPException(409, "Cannot delete a workspace that has child workspaces")

            # CHILD-FIRST: cycle history and messages reference the conversation row,
            # and foreign keys are ON on every connection - clearing the parent first
            # raises IntegrityError and the workspace can never be deleted.
            counts = delete_conversation_rows(cursor, conv_id)
            conn.commit()
        except HTTPException:
            raise  # 404/403 from the ownership guard must not become a 500
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
        """Delete messages from `message_index` onwards (edit / resend).

        `message_index` counts chat-level rows: every messages row except steering rows
        (`is_steer = 1`), which the UI shows inside the turn block instead of as bubbles.
        A steer has no bubble to index, so it is removed by the time window the cut starts at.
        """
        current_ip = get_client_ip(request)
        message_index = data.get("message_index", 0)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            _require_conversation_owner(cursor, conv_id, current_ip)

            cursor.execute(
                "SELECT id, timestamp FROM messages WHERE conversation_id = ? AND is_steer = 0 "
                "ORDER BY timestamp, rowid",
                (conv_id,)
            )
            rows = cursor.fetchall()

            deleted_count = 0
            deleted_steers = 0
            pruned_turn_logs = 0
            if message_index < len(rows):
                ids_to_delete = [row[0] for row in rows[message_index:]]
                deleted_count = len(ids_to_delete)
                from_timestamp = rows[message_index][1]
                placeholders = ",".join("?" * deleted_count)
                cursor.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids_to_delete)
                # Steers of the removed turns: no row of their own in the UI, so they cannot be
                # addressed by index - they follow the time window the cut starts at.
                cursor.execute(
                    "DELETE FROM messages WHERE conversation_id = ? AND is_steer = 1 "
                    "AND timestamp >= ?",
                    (conv_id, from_timestamp),
                )
                deleted_steers = cursor.rowcount
                # Cycle rows belong to the CONVERSATION, not to the answer they describe:
                # without this prune, a reload rebuilds cycle blocks for answers that are gone.
                pruned_turn_logs = truncate_turn_logs(
                    cursor, conv_id, ids_to_delete, from_timestamp
                )
                conn.commit()
                logger.info(
                    f"Client {current_ip} truncated {deleted_count} messages "
                    f"(+{deleted_steers} steers) from conversation {conv_id} "
                    f"(turn_logs={pruned_turn_logs})"
                )
        finally:
            close_db_connection(conn)
        return {"status": "truncated", "deleted_count": deleted_count,
                "deleted_steers": deleted_steers,
                "remaining_count": message_index, "deleted_turn_logs": pruned_turn_logs}

    @router.post("/{conv_id}/reset-state")
    async def reset_conversation_state(conv_id: str, request: Request):
        current_ip = get_client_ip(request)

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            _require_conversation_owner(cursor, conv_id, current_ip)

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
