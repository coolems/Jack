# ---------------------------------------------------------------------------
# Per-turn attached-image registry (2026-08-26)
#
# Lets delivered tool code use the CURRENT TURN's uploaded image bytes DIRECTLY
# without a disk round-trip -- exactly the same pixels that ride on the user
# message for vision. The CLIENT registers them once per turn in logic/agentic.py.
#
# LIFETIME: set at the start of every chat turn and CLEARED when the turn ends
# (set_current_turn_attachments() with empty values). A stale image from an older
# turn must never be transcribed by mistake, so lookups return {} for any filename
# that was not registered on the CURRENT turn.
# ---------------------------------------------------------------------------

_current_turn_attachments: dict = {}


def set_current_turn_attachments(image_data=None, image_paths=None) -> None:
    """Register the images attached to the current chat turn (base64 + saved name).

    Called once per turn by CLIENT logic/agentic.py right before the ReAct loop
    starts. image_data is a list of raw base64 strings and image_paths a parallel
    list of '/files/<savedname>' entries (both come from process_media_files()).
    Passing empty/None values CLEARS the registry -- that is how a turn ends.

    Args:
        image_data: Optional[list[str]] - raw base64 strings, no MIME prefix.
        image_paths: Optional[list[str]] - '/files/<name>' entries matching image_data order.
    """
    global _current_turn_attachments
    mapping = {}
    if image_data and image_paths:
        for b64, path in zip(image_data, image_paths):
            if not isinstance(b64, str) or len(b64) < 100:
                continue
            name = (path or '').split('/')[-1]
            if name:
                mapping[name] = b64
    _current_turn_attachments = mapping


def get_current_turn_attachments() -> dict:
    """Return {saved_filename: base64} for the images attached to the CURRENT turn.

    Used by transcribe_image(): when a model asks to transcribe an image that was
    just uploaded, its pixels are already in memory -- use them directly instead of
    re-reading the file from disk (same bytes, zero I/O). Returns {} for filenames
    not registered on this turn.
    """
    return dict(_current_turn_attachments)

"""Shared utilities module for all COOLEMS tools.

This is the SINGLE SOURCE of truth for utility functions used by tool code.
On SERVER: imported normally via sys.modules['tools.utils'].
On CLIENT: sent as source text and installed in-memory by dynamic_loader.

Contains:
  - Path resolution & validation helpers (delegates to path_guard where needed)
  - File type detection (text vs image extensions)  
  - Image encoding utilities
  - Working root management
  - User role management for tool security guardrails
  - Size limits constants
"""

import base64
import contextvars
import logging
import os
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants used by tools
# ---------------------------------------------------------------------------

MAX_FILE_SIZE: int = 10 * 1024 * 1024              # 10 MB max file read size
MAX_TEXT_CONTENT_SIZE: int = 500_000                # Max chars for text content extraction
MAX_WRITE_SIZE: int = 10 * 1024 * 1024             # 10 MB max write size

TEXT_EXTENSIONS: set = {
    '.txt', '.md', '.py', '.js', '.ts', '.html', '.css', '.json', '.xml',
    '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.sh', '.bat',
    '.ps1', '.sql', '.csv', '.log', '.env', '.gitignore', '.dockerfile',
    '.rst', '.tex', '.c', '.cpp', '.h', '.hpp', '.java', '.go', '.rs',
    '.rb', '.php', '.swift', '.kt', '.scala', '.lua', '.r', '.m',
}

IMAGE_EXTENSIONS: set = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.tiff', '.tif',
}

EXCLUDED_FOLDERS: set = {
    '__pycache__', 'node_modules', '.git', '.venv', 'venv', 'env',
    '.vscode', '.idea', '.pytest_cache', '.mypy_cache',
}


# NOTE (2026-08-19): the Windows extended-length prefix helper that used to live here was
# deleted together with the inline fallback security checks -- it now lives ONLY in the
# canonical module tools/path_guard.py (_strip_extended_length_prefix), which every path check
# delegates to. Keeping a second copy here is how the two implementations drifted.


# ---------------------------------------------------------------------------
# Working root management - PER-WORKSPACE, database as source of truth (2026-10-09)
#
# The old design kept ONE .working_root.json file for the whole process; switching
# workspaces in the UI overwrote it and every other workspace's tools silently broke.
# Now:
#   * Each conversation ("workspace") has its own working_root column in the CLIENT
#     database (conversations.working_root). The CLIENT resolves the active value per
#     turn from that row (app/utils/common/working_root.py) and publishes it here via
#     set_current_working_root() - a contextvars.ContextVar, so concurrent workspaces
#     never see each other's folder.
#   * Fallback: when nothing is set for the current context (no active turn / legacy
#     caller), get_working_root() returns the PROJECT ROOT (the repo containing this
#     tools/ directory) - a valid, stable default instead of a crash.
# This module never reads or writes .working_root.json anymore; that file no longer
# exists.

def _project_root_fallback() -> str:
    """Project root = parent of the tools/ directory (the repo this module ships in)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_working_root_ctx = contextvars.ContextVar("_current_working_root", default=None)
# LEGACY FALLBACK (mirrors the user-role store below): last value written process-wide;
# consulted only when the current asyncio context has no working root of its own.
_current_working_root: Optional[str] = None

def set_current_working_root(path) -> None:
    """Publish *path* as the working root for the CURRENT turn/context (None clears it).

    Called once per chat turn by CLIENT logic/agentic_mode() with the value resolved
    from that conversation's DB row. contextvars carry it into asyncio.to_thread() and
    create_task(), so every tool executed during the turn - sync or async - resolves
    exactly this workspace's folder, even when several workspaces run concurrently.

    Args:
        path: Absolute directory to use for this turn, or None to clear back to fallback.
    """
    global _current_working_root
    resolved = os.path.normpath(os.path.abspath(path)) if path else None
    _working_root_ctx.set(resolved)
    _current_working_root = resolved  # legacy fallback for non-async callers/tests

def get_working_root() -> str:
    """Return the working root for the CURRENT context.

    Resolution order (2026-10-09):
      1. The ContextVar of the current task/context - set per turn from the active
         workspace's conversations.working_root DB row (per-workspace isolation).
      2. Legacy module global (non-async callers / direct test sets).
      3. Project root fallback - valid default when no workspace value is active yet.

    Never raises: a missing working root must not break tool execution.
    """
    ctx_wr = _working_root_ctx.get()
    if ctx_wr and os.path.isdir(ctx_wr):
        return ctx_wr
    if _current_working_root and os.path.isdir(_current_working_root):
        return _current_working_root
    fallback = _project_root_fallback()
    logger.debug(f"Working root not set for this context - using project root: {fallback}")
    return fallback

def set_working_root(path) -> None:
    """Set the working root for the current context (tests / direct callers).

    The production writer is CLIENT-side: POST /api/working_root persists per-workspace
    values to the database and agentic_mode() publishes them via
    set_current_working_root(). This function only updates THIS process's context value -
    it never touches disk.

    Raises:
        ValueError: if *path* is empty or not an existing directory.
    """
    if not path or not str(path).strip():
        raise ValueError("Cannot set working root: empty path")
    resolved = os.path.abspath(str(path))
    if not os.path.isdir(resolved):
        raise ValueError(f"Cannot set working root: directory does not exist: {resolved}")
    set_current_working_root(resolved)

# ---------------------------------------------------------------------------
# User role store (ContextVar + legacy global)
#
# FIX (2026-05-28): Added to resolve admin role correctly in python_exec tool.
# Previously missing, causing all users to default to "user" role and get
# blocked by security guardrails even when authenticated as admin.
#
# CURRENT ARCHITECTURE (verified 2026-08-28): the SERVER is a just relay -- it
# never executes tools; every tool runs in the CLIENT sandbox, where the profile
# role arrives as the injected global CURRENT_USER_ROLE (shipped in
# tools_response.config_constants). This store therefore has NO live production
# consumer: it exists for tests and direct callers that need an isolated,
# per-task role value.
#
# FIX (2026-08-19) - PER-TASK ISOLATION via contextvars:
#   The old design kept ONE module-level value for the whole process, so with two
#   concurrent clients the LAST authenticated identity silently applied to
#   EVERYONE's tool execution. A contextvars.ContextVar carries the value along
#   with the calling task/context instead -- concurrent contexts get isolated
#   values automatically. The module-level global is kept as a LEGACY FALLBACK
#   ONLY: it still serves non-async callers and existing tests that set it
#   directly from a plain thread.
# ---------------------------------------------------------------------------

_current_user_role_ctx = contextvars.ContextVar("_current_user_role", default=None)
# LEGACY FALLBACK (see header): last value written process-wide; consulted only when
# the current asyncio context has no role of its own. Never a source for async callers.
_current_user_role: Optional[str] = None


def set_current_user_role(role: str) -> None:
    """Store a user role in this store (see header).

        In the live architecture no production code path calls this: CLIENT-side
        tools read their role from the injected CURRENT_USER_ROLE global. Kept for
        tests and direct callers that need a per-task isolated role value.

    FIX (2026-08-19): writes BOTH the per-task ContextVar (the value that async
    callers actually read -- isolated per WS connection) AND the legacy module
    global (kept so non-async callers/tests keep working). In concurrent
    asyncio contexts only the ContextVar matters, which is what prevents role
    bleed between simultaneous clients.

    Args:
        role: The user's role string (e.g., 'admin', 'power_user', 'user')
    """
    global _current_user_role
    _current_user_role_ctx.set(role)
    _current_user_role = role  # legacy fallback for non-async callers/tests
    logger.debug(f"Current user role set to: {role}")


def get_current_user_role() -> str:
    """Return the current authenticated user's role.

    Resolution order (FIX 2026-08-19):
      1. The ContextVar of the CURRENT task/context -- per-connection value set at
         auth time; concurrent WS clients can never see each other's roles.
      2. Legacy module global (non-async callers / direct test sets).
      3. 'user' -- safe default when nothing has been set (e.g., CLIENT sandbox
         without server context).

    Returns:
        Role string: 'admin', 'power_user', or 'user'
    """
    ctx_role = _current_user_role_ctx.get()
    if ctx_role is not None:
        return ctx_role
    if _current_user_role is not None:
        return _current_user_role
    # Safe default - unauthenticated tools run with user restrictions
    return "user"


# ---------------------------------------------------------------------------
# python_exec blocked-libs set store (per-profile constraints shipped from SERVER)
    # FIX (2026-08-20): profiles.json references a blocked-libs JSON file per role.
    # The SERVER loads it and ships the list with tools_response
    # ("python_exec_blocked_libs"); on the CLIENT -- where python_exec actually
    # executes -- the value arrives as the injected global PYTHON_EXEC_BLOCKED_LIBS
    # (dynamic_loader compiles tools with config_constants in globals()).
    # CURRENT ARCHITECTURE (verified 2026-08-28): the SERVER never executes tools, so
    # this store has NO live production consumer; it exists for tests and direct
    # callers. The broken path-scope-guard sibling of these functions was removed on
    # 2026-08-28 (its ContextVar/global were never defined -> NameError on every
    # call, and no profile key or injection ever fed it).
#
# FIX (2026-08-19) - PER-CONNECTION ISOLATION via contextvars:
#   Same treatment as the role store above. The per-task ContextVar is the value
#   async callers read (isolated per WS connection); the module-level global stays
#   only as a legacy fallback for non-async callers/tests. Without this, one
#   client's profile set (e.g. an admin's EMPTY allow-all list) would leak into
#   every other concurrent client's python_exec guardrails.
# ---------------------------------------------------------------------------

_current_python_exec_blocked_libs_ctx = contextvars.ContextVar(
    "_current_python_exec_blocked_libs", default=None
)
# LEGACY FALLBACK (see header): last value written process-wide; consulted only when
# the current asyncio context has no set of its own. Never a source for async callers.
_current_python_exec_blocked_libs: Optional[list] = None


def _clean_blocked_libs(modules) -> list:
    """Normalize a blocked-libs input to a clean list of module-name strings."""
    return [str(m).strip() for m in modules if isinstance(m, str) and m.strip()]


def set_python_exec_blocked_libs(modules) -> None:
    """Store a python_exec blocked-libs list in this store (see header).

        Accepts a list of module names; an EMPTY list is meaningful -- it means "no
        restrictions at all" and is stored as-is. Pass None to clear back to the
        built-in defaults. Live CLIENT tools read their set from the injected
        PYTHON_EXEC_BLOCKED_LIBS global, not from this store.

    FIX (2026-08-19): writes BOTH the per-task ContextVar (what async callers read)
    AND the legacy module global, mirroring set_current_user_role().
    """
    global _current_python_exec_blocked_libs
    if modules is None:
        cleaned = None
    else:
        cleaned = _clean_blocked_libs(modules)
    _current_python_exec_blocked_libs_ctx.set(cleaned)
    _current_python_exec_blocked_libs = cleaned  # legacy fallback for non-async callers/tests
    logger.debug(f"python_exec blocked-libs set updated: {len(_current_python_exec_blocked_libs or [])} module(s)")


def get_python_exec_blocked_libs():
    """Return the stored python_exec blocked-libs list, or None for built-in defaults.

    Resolution order (FIX 2026-08-19): ContextVar of the current task first
    (per-connection isolation), then the legacy module global, then None.

    Returns:
        list -- module names to block on import (empty list = allow everything)
        None -- no per-profile set available -> caller uses its built-in default blocklist
    """
    stored = _current_python_exec_blocked_libs_ctx.get()
    if stored is not None:
        return stored
    return _current_python_exec_blocked_libs

# NOTE (2026-08-28): the former set/get_python_exec_path_scope_guard() pair was
    # removed -- its ContextVar and module global were never defined in this module,
    # so both functions raised NameError on every call. Nothing ever wired them up:
    # no profile key exists, no tools_response field ships such a flag, and the
    # CLIENT injects no PYTHON_EXEC_PATH_SCOPE_GUARD global. The path-scope guardrail
    # is gated by the per-profile blocked-libs set alone (see python_exec.py).

# ---------------------------------------------------------------------------

def validate_filename(filename: str, label: str = "Filename") -> None:
    """Validate that a filename contains no path traversal or separators.
    
    Args:
        filename: The filename to validate.
        label: Label for error messages.
        
    Raises:
        ValueError: If the filename is invalid.
    """
    if not filename or not filename.strip():
        raise ValueError(f"{label} cannot be empty")
    
    # Strip whitespace
    cleaned = filename.strip()
    
    # Check for path separators (prevent directory traversal)
    if os.sep in cleaned:
        raise ValueError(f"{label} must not contain path separators: {filename!r}")
    if '/' in cleaned:
        raise ValueError(f"{label} must not contain forward slashes: {filename!r}")
    
    # Check for .. traversal
    if '..' in cleaned.split(os.sep):
        raise ValueError(f"{label} must not contain '..': {filename!r}")
    
    # Reject null bytes and control characters
    if '\x00' in cleaned:
        raise ValueError(f"{label} contains null byte")


def resolve_path_to_dir(working_root: str, path: str = "") -> str:
    """Resolve *path* to a directory inside *working_root*.
    
    Delegates to path_guard.resolve_path_to_dir for security validation.
    
    Args:
        working_root: The base allowed directory.
        path: Relative or absolute path to resolve. Empty string means working_root itself.
        
    Returns:
        Resolved absolute directory path.
        
    Raises:
        ValueError: If the resolved path escapes working_root.
    """
    try:
        from tools.path_guard import resolve_path_to_dir as _pg_resolve
        return _pg_resolve(working_root, path)
    except ImportError:
        # FAIL-CLOSED (2026-08-19): no inline re-implementation. A weaker second copy of the
        # containment check would silently drift from the canonical one in tools/path_guard.py
        # (this exact divergence already happened: this fallback lacked _has_leading_drive_letter
        # and returned un-normalized paths). path_guard is delivered to the CLIENT via
        # shared_sources, so a missing module means a broken deployment -- refuse loudly.
        raise RuntimeError(
            "path security module 'tools.path_guard' is unavailable - refusing to resolve "
            f"path {path!r} without canonical validation (fail-closed). Check that shared "
            "modules were delivered/installed correctly."
        )

def guard_path_inside_working_root(
    filepath: str,
    working_root: Optional[str] = None,
    label: str = "File path",
) -> None:
    """Ensure *filepath* is inside *working_root*. Raises ``ValueError`` if not.
    
    Delegates to path_guard.guard_path_inside_working_root for security validation.
    """
    wr = working_root or get_working_root()
    try:
        from tools.path_guard import guard_path_inside_working_root as _pg_guard
        _pg_guard(filepath, wr, label)
    except ImportError:
        # FAIL-CLOSED (2026-08-19): see resolve_path_to_dir above -- no weaker inline copy.
        raise RuntimeError(
            "path security module 'tools.path_guard' is unavailable - refusing to validate "
            f"{label} {filepath!r} without canonical validation (fail-closed)."
        )


# ---------------------------------------------------------------------------
# File type detection
# ---------------------------------------------------------------------------

def is_text_file(filepath: str) -> bool:
    """Check if a file has a text-like extension."""
    ext = os.path.splitext(filepath)[1].lower()
    return ext in TEXT_EXTENSIONS


def is_image_file(filepath: str) -> bool:
    """Check if a file has an image extension."""
    ext = os.path.splitext(filepath)[1].lower()
    return ext in IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# Image utilities
# ---------------------------------------------------------------------------

def encode_image_to_base64(image_path: str) -> Optional[str]:
    """Read an image file and return its base64-encoded string.
    
    Args:
        image_path: Absolute path to the image file.
        
    Returns:
        Base64 encoded string of the file content, or None on failure.
    """
    try:
        with open(image_path, 'rb') as f:
            data = f.read(MAX_FILE_SIZE)
        return base64.b64encode(data).decode('ascii')
    except Exception as e:
        logger.error(f"Failed to encode image {image_path}: {e}")
        return None


def read_text_file(filepath: str, max_chars: int = MAX_TEXT_CONTENT_SIZE) -> Optional[str]:
    """Read a text file with size limit.
    
    Args:
        filepath: Absolute path to the text file.
        max_chars: Maximum characters to read (default 500k).
        
    Returns:
        File content as string, or None on failure.
    """
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(max_chars)
        return content
    except Exception as e:
        logger.error(f"Failed to read text file {filepath}: {e}")
        return None


def get_file_info(filepath: str) -> dict:
    """Get basic file metadata.
    
    Returns a dict with keys: name, ext, size_bytes, is_text, is_image.
    """
    stat = os.stat(filepath) if os.path.exists(filepath) else None
    
    return {
        'name': os.path.basename(filepath),
        'ext': os.path.splitext(filepath)[1].lower(),
        'size_bytes': stat.st_size if stat else 0,
        'is_text': is_text_file(filepath),
        'is_image': is_image_file(filepath),
    }


# ---------------------------------------------------------------------------
# File search (delegates to path_guard.secure_find_file)
# ---------------------------------------------------------------------------

def find_file(
    filename: str,
    working_root: Optional[str] = None,
    project_root: Optional[str] = None,
) -> Optional[str]:
    """Search for *filename* inside allowed directories.
    
    Delegates to path_guard.secure_find_file which never returns paths outside
    the allowed directories (defense in depth).
    
    Args:
        filename: Name of the file to find (may include relative subpath).
        working_root: Base directory for search. Uses global if None.
        project_root: Secondary search directory (optional).
        
    Returns:
        Absolute path to found file, or None if not found.
    """
    wr = working_root or get_working_root()
    
    try:
        from tools.path_guard import secure_find_file as _pg_find
        return _pg_find(filename, wr, project_root)
    except ImportError:
        # FAIL-CLOSED (2026-08-19): see resolve_path_to_dir above -- no weaker inline copy.
        raise RuntimeError(
            "path security module 'tools.path_guard' is unavailable - refusing to search for "
            f"file {filename!r} without canonical validation (fail-closed)."
        )


# ---------------------------------------------------------------------------
# File access validation (delegates to path_guard.validate_file_access)
# ---------------------------------------------------------------------------

def validate_file_access(
    filepath: str,
    working_root: Optional[str] = None,
    must_exist: bool = True,
    label: str = "File",
) -> str:
    """Validate that a file can be safely accessed.
    
    Delegates to path_guard.validate_file_access for security validation.
    
    Args:
        filepath: Path to validate (relative or absolute).
        working_root: Base allowed directory. Uses global if None.
        must_exist: If True, the file must exist on disk.
        label: Label for error messages.
        
    Returns:
        Resolved absolute path string.
        
    Raises:
        ValueError: If access is denied or file doesn't exist when required.
    """
    wr = working_root or get_working_root()
    
    try:
        from tools.path_guard import validate_file_access as _pg_validate
        # path_guard.validate_file_access takes (filepath, working_root, label)
        # It ALWAYS checks existence internally, so must_exist is handled by the guard itself
        return _pg_validate(filepath, wr, label)
    except ImportError:
        # FAIL-CLOSED (2026-08-19): see resolve_path_to_dir above -- no weaker inline copy.
        raise RuntimeError(
            "path security module 'tools.path_guard' is unavailable - refusing to validate "
            f"{label} {filepath!r} without canonical validation (fail-closed)."
        )
