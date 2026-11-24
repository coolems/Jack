"""Workspace ID allocation, migration, and hierarchy rules."""

import re
import secrets
import string

from fastapi import HTTPException

MAX_WORKSPACE_DEPTH = 3
WORKSPACE_ID_LENGTH = 7
_ASCII_LETTER_RANGES = "a-zA-Z"
_WORKSPACE_ID_RE = re.compile(rf"[{_ASCII_LETTER_RANGES}]{{{WORKSPACE_ID_LENGTH}}}")
_INVALID_ID = (
    "{id} IS NULL OR typeof({id}) != 'text' "
    f"OR length({{id}}) != {WORKSPACE_ID_LENGTH} OR instr({{id}}, char(0)) > 0 "
    f"OR {{id}} GLOB '*[^{_ASCII_LETTER_RANGES}]*'"
)


def is_workspace_id(value) -> bool:
    """True only for exactly WORKSPACE_ID_LENGTH ASCII letters; any other type is False."""
    return isinstance(value, str) and _WORKSPACE_ID_RE.fullmatch(value) is not None


def allocate_workspace_id(cursor):
    """Allocate against all owners; the caller must hold the SQLite write lock."""
    while True:
        workspace_id = ''.join(secrets.choice(string.ascii_lowercase) for _ in range(WORKSPACE_ID_LENGTH))
        if not cursor.execute("SELECT 1 FROM conversations WHERE id = ?", (workspace_id,)).fetchone():
            return workspace_id


def migrate_workspace_ids(cursor):
    """Rewrite legacy IDs and relational references in the startup transaction."""
    if not cursor.connection.in_transaction:
        raise RuntimeError("Workspace ID migration requires an active transaction")
    cursor.execute("PRAGMA defer_foreign_keys = ON")
    old_ids = cursor.execute(
        "SELECT id FROM conversations WHERE " + _INVALID_ID.format(id="id")
    ).fetchall()
    for (old_id,) in old_ids:
        new_id = allocate_workspace_id(cursor)
        cursor.execute("UPDATE conversations SET id = ? WHERE id IS ?", (new_id, old_id))
        if old_id is not None:
            cursor.execute("UPDATE conversations SET parent_id = ? WHERE parent_id = ?", (new_id, old_id))
            cursor.execute("UPDATE messages SET conversation_id = ? WHERE conversation_id = ?", (new_id, old_id))
            cursor.execute("UPDATE turn_log SET conversation_id = ? WHERE conversation_id = ?", (new_id, old_id))

    # Guard other database writers as well (including legacy implicit creation paths).
    invalid_new_id = _INVALID_ID.format(id="NEW.id")
    for event, suffix in [("INSERT", "insert"), ("UPDATE OF id", "update")]:
        cursor.execute(f"DROP TRIGGER IF EXISTS workspace_id_{suffix}")
        cursor.execute(f"""
            CREATE TRIGGER workspace_id_{suffix}
            BEFORE {event} ON conversations
            WHEN {invalid_new_id}
            BEGIN
                SELECT RAISE(ABORT, 'Workspace ID must contain exactly seven ASCII letters');
            END
        """)


def validate_workspace_parent(cursor, conv_id, owner_ip, parent_id):
    """Validate the destination and the full moved branch under a write lock."""
    if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
        raise HTTPException(422, "parent_id must be a workspace ID or null")

    parent_depth = 0
    seen = {conv_id}
    ancestor_id = parent_id
    while ancestor_id is not None:
        if ancestor_id in seen:
            raise HTTPException(409, "Cannot move a workspace into itself or its descendants")
        seen.add(ancestor_id)
        row = cursor.execute(
            "SELECT ip_address, parent_id FROM conversations WHERE id = ?", (ancestor_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Parent workspace not found")
        if row[0] != owner_ip:
            raise HTTPException(403, "Parent workspace belongs to another client")
        parent_depth += 1
        ancestor_id = row[1]

    # Repair over-deep trees by moving a smaller descendant branch out first.
    # A fourth level is enough to reject this move; no need to traverse deeper.
    height = cursor.execute("""
        WITH RECURSIVE branch(id, level) AS (
            SELECT ?, 1
            UNION ALL
            SELECT child.id, branch.level + 1
            FROM conversations AS child JOIN branch ON child.parent_id = branch.id
            WHERE branch.level <= ?
        )
        SELECT MAX(level) FROM branch
    """, (conv_id, MAX_WORKSPACE_DEPTH)).fetchone()[0]
    if parent_depth + height > MAX_WORKSPACE_DEPTH:
        raise HTTPException(409, "Workspace nesting cannot exceed three levels")


