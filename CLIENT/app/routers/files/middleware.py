"""Shared helpers for file router endpoints."""

import os
from datetime import datetime
import logging

logger = logging.getLogger("COOLEMS")


def _safe_relative_path(base: str, rel: str):
    """Compute a safe full path under base, rejecting traversal escapes.
    
    Uses realpath to resolve symlinks and .. sequences before containment check.
    Returns None if the resolved path escapes *base*.

    2026-11-14 review fix A5 (same class as path_guard): compare with normcase so a
    case-different spelling of the root still contains its own files, and rstrip the
    root before appending os.sep so a drive root ('C:\\') does not become 'C:\\\\'.
    """
    if not rel:
        return base
    full = os.path.realpath(os.path.join(base, rel))
    # Compare BOTH sides normcase'd -- realpath returns the on-disk case, so a
    # case-different root must not deny its own children.
    base_real = os.path.normcase(os.path.realpath(base))
    full_norm = os.path.normcase(full)
    prefix = base_real.rstrip(os.sep) + os.sep
    if not (full_norm.startswith(prefix) or full_norm == base_real):
        return None
    return full


def _is_root_itself(base: str, resolved: str) -> bool:
    """True when *resolved* IS the working root directory itself.

    SECURITY (2026-11-02): the containment check above deliberately ACCEPTS the
    root, because listing/uploading at the root is legitimate. But a destructive
    endpoint must not accept it: rel='.', ' ', './.', 'sub/../' and a symlink that
    points back at the root all realpath() to *base*, so os.path.isdir() would pass
    and shutil.rmtree(base) would wipe the entire workspace. Destructive endpoints
    call this before deleting and reject with 400.

    Compared on realpath + normcase so Windows case-insensitivity and symlink
    indirection cannot smuggle the root past the check.
    """
    return os.path.normcase(os.path.realpath(resolved)) == os.path.normcase(os.path.realpath(base))


def _get_working_root_dir():
    """Return the current dynamic working root directory.
    
    Raises RuntimeError if working_root cannot be determined — NO silent fallback.
    This ensures we NEVER operate on an unintended directory.
    """
    try:
        from app.utils.common import get_working_root
        wr = get_working_root()
        if not wr or not os.path.isdir(wr):
            raise RuntimeError(f"working_root is not a valid directory: {wr!r}")
        return wr
    except Exception as e:
        logger.error(f"Failed to resolve working_root: {e}")
        raise RuntimeError(
            f"Cannot determine working_root. Ensure the application is initialized properly. Error: {e}"
        ) from e


def _format_datetime(timestamp):
    """Format a file modification timestamp."""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
