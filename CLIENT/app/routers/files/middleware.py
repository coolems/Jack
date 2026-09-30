"""Shared helpers for file router endpoints."""

import os
from datetime import datetime
import logging

logger = logging.getLogger("COOLEMS")


def _safe_relative_path(base: str, rel: str):
    """Compute a safe full path under base, rejecting traversal escapes.
    
    Uses realpath to resolve symlinks and .. sequences before containment check.
    Returns None if the resolved path escapes *base*.
    """
    if not rel:
        return base
    full = os.path.realpath(os.path.join(base, rel))
    base_real = os.path.realpath(base)
    if not (full.startswith(base_real + os.sep) or full == base_real):
        return None
    return full


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
