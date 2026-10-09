"""Working root management - PER-WORKSPACE, database as source of truth (2026-10-09).

CONTRACT (rewritten 2026-10-09):
  * The ONLY persistence for working roots is the CLIENT database: each conversation
    ("workspace") has its own ``conversations.working_root`` column. There is NO disk
    file anymore - the old single ``CLIENT/config/.working_root.json`` was removed
    because it held ONE value for the whole process, so switching workspaces in the UI
    overwrote it and silently broke every other workspace's tools.
  * Resolution per conversation: :func:`resolve_working_root_for_conv` returns the row's
    stored path when it still exists as a directory, otherwise the PROJECT ROOT (parent
    of CLIENT/) - a valid default until the user sets that workspace's own folder in the
    UI ("Working Folder" chip).
  * There is exactly ONE in-memory variable holding the ACTIVE value per process:
    ``_WORKING_ROOT_CACHE``. It mirrors whichever workspace the UI has open right now,
    so HTTP file endpoints (which have no conversation context) operate on the folder
    of the currently viewed workspace. No other module may keep its own copy - they
    must call :func:`get_working_root`.
  * Tools never read this cache: agentic_mode() resolves the active turn's conversation
    row and publishes it via tools.utils.set_current_working_root() (a ContextVar), so
    concurrent workspaces execute against their OWN folders.
"""

import logging
import os

logger = logging.getLogger("COOLEMS.Tools")


# This module sits at CLIENT/app/utils/common/ -> three levels up reach <CLIENT>.
_CLIENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# Fallback for workspaces without a stored value: the project root (parent of CLIENT/).
_PROJECT_ROOT = os.path.dirname(_CLIENT_DIR)

# The ONE in-memory variable holding the ACTIVE working root (the workspace open in the UI).
_WORKING_ROOT_CACHE: str | None = None


def _project_root() -> str:
    """Project root - valid default for any workspace without its own stored value."""
    return _PROJECT_ROOT if os.path.isdir(_PROJECT_ROOT) else os.getcwd()


# ---------------------------------------------------------------------------
# Database access (per-workspace rows)
# ---------------------------------------------------------------------------

def _write_conversation_working_root(conv_id: str, path: str | None) -> bool:
    """Write *path* onto the conversation row (None = clear). Returns True on success.

    Never raises - bookkeeping must not break the working-root change itself.
    The row is created when missing so a fresh workspace can pin its folder immediately.
    """
    if not conv_id or not str(conv_id).strip():
        return False
    try:
        from app.db_manager import get_db_connection, close_db_connection
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM conversations WHERE id = ?", (str(conv_id),))
            if cursor.fetchone() is None:
                logger.warning(
                    "Conversation %s missing in DB - creating row so working_root can be persisted",
                    str(conv_id)[:8],
                )
                cursor.execute(
                    "INSERT INTO conversations (id, title) VALUES (?, 'New Workspace')",
                    (str(conv_id),),
                )
            cursor.execute(
                "UPDATE conversations SET working_root = ? WHERE id = ?",
                (path, str(conv_id)),
            )
            conn.commit()
            return True
        finally:
            close_db_connection(conn)
    except Exception as e:
        logger.warning(f"Could not persist working_root for conversation {conv_id}: {e}")
        return False


def get_conversation_working_root(conv_id: str | None) -> str | None:
    """Return the stored working root of one conversation, or None.

    Returns the absolute path only when the row has a value AND it still exists as a
    directory; every other case (no conv_id, missing row, NULL/empty value, vanished
    folder) yields None so callers can fall back to the project root. Never raises.
    """
    if not conv_id or not str(conv_id).strip():
        return None
    try:
        from app.db_manager import get_db_connection, close_db_connection
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT working_root FROM conversations WHERE id = ?", (str(conv_id).strip(),)
            )
            row = cursor.fetchone()
        finally:
            close_db_connection(conn)
    except Exception as e:
        logger.debug(f"Could not read working_root for conversation {conv_id}: {e}")
        return None

    if not row or not row[0]:
        return None
    saved_path = str(row[0]).strip()
    if not saved_path:
        return None
    resolved = os.path.abspath(saved_path)
    if not os.path.isdir(resolved):
        logger.warning(
            "Stored working root for conversation %s does not exist: %s - falling back to project root",
            str(conv_id)[:8], resolved,
        )
        return None
    return resolved


def resolve_working_root_for_conv(conv_id: str | None) -> str:
    """Resolve the effective working root for a conversation (workspace).

    The stored per-workspace value when valid, otherwise the project root fallback.
    This is what agentic_mode() publishes to tools.utils for each turn and what
    POST /api/working_root uses to "activate" a workspace's folder.
    """
    return get_conversation_working_root(conv_id) or _project_root()


# ---------------------------------------------------------------------------
# Active (UI-visible) working root
# ---------------------------------------------------------------------------

def get_working_root() -> str:
    """Return the ACTIVE working root - the folder of the workspace open in the UI.

    Resolution order:
      1. ``_WORKING_ROOT_CACHE`` when it still points at an existing directory.
      2. Project root fallback (a fresh process before any workspace was activated).

    The returned path is ALWAYS a real, existing directory. Tools do NOT use this
    function for per-turn resolution - they read the ContextVar published by
    agentic_mode() from their own conversation row (see module docstring).
    """
    global _WORKING_ROOT_CACHE
    if _WORKING_ROOT_CACHE:
        try:
            if os.path.isdir(_WORKING_ROOT_CACHE):
                return _WORKING_ROOT_CACHE
        except Exception:
            pass
    fallback = _project_root()
    _WORKING_ROOT_CACHE = fallback
    logger.info("Working root not activated yet - using project root: %s", fallback)
    return _WORKING_ROOT_CACHE


def set_working_root(path: str, conv_id: str | None = None) -> str:
    """Activate *path* as the current working root (UI action).

    Updates the in-memory active value. When *conv_id* is given the value is ALSO
    persisted onto that conversation's row - that DB row is the per-workspace source of
    truth; switching back to this workspace later restores exactly this folder.

    Parameters
    ----------
    path : str
        Directory path (absolute or relative to CWD). Must point at an existing
        directory, unless *conv_id* is given and *path* is empty - then the
        conversation's OWN stored value (or project root fallback) is activated
        instead. This is how "open workspace" pins a fresh workspace to its folder.

    Returns
    -------
    str
        The active working root after this call.

    Raises
    ------
    ValueError
        If *path* is non-empty but does not exist as a directory.
    """
    global _WORKING_ROOT_CACHE

    if path and str(path).strip():
        resolved = os.path.abspath(str(path))
        if not os.path.isdir(resolved):
            raise ValueError(f"Cannot set working root: directory does not exist: {resolved}")
    elif conv_id:
        # Activate the workspace's own stored value (project-root fallback for fresh ones).
        resolved = resolve_working_root_for_conv(conv_id)
    else:
        raise ValueError("Cannot set working root: empty path and no conversation_id")

    _WORKING_ROOT_CACHE = resolved
    if conv_id:
        _write_conversation_working_root(conv_id, resolved)
    logger.info(
        "Working root activated%s: %s",
        f" (persisted for conversation {str(conv_id)[:8]})" if conv_id else "",
        resolved,
    )
    return resolved


# ---------------------------------------------------------------------------
# Backward compatibility - allows ``from common import WORKING_ROOT`` style access
# ---------------------------------------------------------------------------

def __getattr__(name):
    if name == "WORKING_ROOT":
        return get_working_root()
    raise AttributeError("module %r has no attribute '%s'" % (__name__, name))
