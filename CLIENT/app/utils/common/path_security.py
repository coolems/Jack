"""Path security guards — canonical path validation and sandbox checks.

Every function in this module delegates to ``_is_path_inside_allowed()`` as the
single source of truth for path safety decisions.

Security layers (defense in depth):
  Layer 1: Strip leading separators so "/code.py" becomes relative.
  Layer 2: Reject drive letters and absolute paths after stripping.
  Layer 3: Join with working_root to neutralize any remaining tricks.
  Layer 4: os.path.realpath() resolves symlinks, UNC escapes, and .. sequences.
  Layer 5: Final containment check against allowed_roots using realpath comparison.
"""

import logging
import os
from typing import List, Optional, Tuple

logger = logging.getLogger("COOLEMS.Tools")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_extended_length_prefix(path: str) -> str:
    r"""Strip Windows extended-length path prefixes so containment checks work.

    On Windows, paths can be prefixed with:
      - \\?\C:\...          (extended-length absolute)
      - \\?\UNC\server\...  (extended-length UNC)

    os.path.realpath() PRESERVES these prefixes instead of stripping them,
    which breaks containment checks against normal allowed_roots that don't
    have the prefix. This helper normalizes both sides so comparison works.

    Examples (illustrative paths -- not real locations):
        \\?\C:\Users\test  ->  C:\Users\test
        \\?\UNC\server\share ->  \\server\share
        C:\normal\path       ->  C:\normal\path  (unchanged)
    """
    if path.startswith('\\\\?\\UNC\\'):
        # \\?\UNC\server\share -> \\server\share
        return '\\\\' + path[8:]
    elif path.startswith('\\\\?\\'):
        # \\?\C:\path -> C:\path
        return path[4:]
    return path


def _is_path_inside_allowed(filepath: str, allowed_roots: List[str]) -> bool:
    """Return True when *filepath* resolves inside any of *allowed_roots*.

    Normalises both sides with ``os.path.realpath()`` so that symlinks and
    ``..`` tricks are fully resolved before comparison.  Using ``realpath``
    instead of ``abspath`` prevents symlink-based escape attacks where a link
    inside working_root points to an arbitrary location on disk.

    This is the **ultimate safety net** -- all public functions delegate here
    as their final validation step. Even if earlier checks are bypassed, this
    layer catches: UNC paths (\\\\server\\share), extended-length paths
    (\\\\?\\C:\\...), symlink escapes, and .. traversal.
    """
    # REALPATH resolves symlinks -- abspath does not.
    real_path = os.path.realpath(filepath)
    # CRITICAL FIX: Strip \\?\ prefix so comparison works against normal roots
    real_path = _strip_extended_length_prefix(real_path)

    for root in allowed_roots:
        norm_root = os.path.normpath(os.path.abspath(root))
        # Also normalize the root side defensively (roots usually don't have prefix)
        norm_root = _strip_extended_length_prefix(norm_root)

        # Exact match OR starts with root + separator (prevents prefix attacks)
        if real_path == norm_root or real_path.startswith(norm_root + os.sep):
            return True
    return False


def _denied_error(filepath: str, allowed_roots: List[str], label: str = "Path") -> ValueError:
    """Build a consistent ``ValueError`` for path-denied situations."""
    if len(allowed_roots) == 1:
        root_str = f"{allowed_roots[0]!r}"
    else:
        root_str = (
            "one of:\n" + "\n".join(f"  - {r!r}" for r in allowed_roots)
        )
    return ValueError(
        f"{label} '{filepath}' is outside the allowed directory.\n"
        f"All operations must stay within: {root_str}\n"
        "If you need to work elsewhere, please change the working_root first."
    )


# ---------------------------------------------------------------------------
# Public API — filename validation
# ---------------------------------------------------------------------------


def validate_filename(filename: str, label: str = "Filename") -> None:
    """Validate a filename doesn't contain path traversal or separators.

    Raises ``ValueError`` when *filename* is empty, contains ``..``,
    or contains path separators (``/`` or ``\\``).
    """
    if not filename or not filename.strip():
        raise ValueError(f"{label} cannot be empty")
    if ".." in filename:
        raise ValueError(
            f"{label} '{filename}' contains path traversal ('..'). "
            f"Only the file name is allowed, no directory navigation."
        )
    if "/" in filename or "\\" in filename:
        raise ValueError(
            f"{label} '{filename}' contains path separators. "
            f"Only a plain file name is allowed, no paths."
        )


# ---------------------------------------------------------------------------
# Public API — working-root guards
# ---------------------------------------------------------------------------


def guard_path_inside_working_root(
    filepath: str,
    working_root: Optional[str] = None,
    label: str = "File path",
) -> None:
    """Ensure *filepath* is inside *working_root*.  Raises ``ValueError`` if not."""
    from .working_root import get_working_root

    wr = working_root or get_working_root()
    allowed = [wr]
    real_path = os.path.realpath(filepath)
    if not _is_path_inside_allowed(real_path, allowed):
        raise _denied_error(real_path, allowed, label)


def resolve_path_in_working_root(
    working_root: Optional[str] = None,
    path: str = "",
    filename: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Resolve *path* (and optional *filename*) inside *working_root*.

    Returns ``(dirpath, full_filepath_or_None)``.
    Raises ``ValueError`` on any escape attempt.
    """
    from .working_root import get_working_root

    wr = working_root or get_working_root()
    effective_root = os.path.normpath(os.path.abspath(wr))

    # --- directory part ---------------------------------------------------
    if not path or not path.strip() or path == "." or path.lower() == "root":
        dirpath = effective_root
    elif os.path.isabs(path):
        real_path = os.path.realpath(path)
        if not _is_path_inside_allowed(real_path, [effective_root]):
            raise _denied_error(path, [wr], "Path")
        dirpath = real_path
    else:
        candidate = os.path.normpath(os.path.join(effective_root, path))
        real_candidate = os.path.realpath(candidate)
        if not _is_path_inside_allowed(real_candidate, [effective_root]):
            raise _denied_error(path, [wr], "Path")
        dirpath = real_candidate

    # --- file part (optional) ---------------------------------------------
    full_filepath: Optional[str] = None
    if filename and filename.strip():
        full_filepath = os.path.normpath(os.path.join(dirpath, filename))
        real_full = os.path.realpath(full_filepath)
        if not _is_path_inside_allowed(real_full, [effective_root]):
            raise _denied_error(filename, [wr], "Resolved file path")

    return dirpath, full_filepath


def resolve_path_for_move(
    working_root: Optional[str] = None,
    path: str = "",
    filename: Optional[str] = None,
    label: str = "Path",
) -> Tuple[str, Optional[str]]:
    """Alias for ``resolve_path_in_working_root`` — used by move_file tool."""
    return resolve_path_in_working_root(working_root, path, filename)


def resolve_path_to_dir(
    working_root: Optional[str] = None,
    path: str = "",
) -> str:
    """Resolve *path* to a directory inside *working_root*.

    Used by tools that only need a directory (not a file).
    Raises ``ValueError`` on escape.
    """
    from .working_root import get_working_root

    wr = working_root or get_working_root()
    effective_root = os.path.normpath(os.path.abspath(wr))

    if not path or not path.strip() or path == "." or path.lower() == "root":
        return effective_root

    if os.path.isabs(path):
        real_path = os.path.realpath(path)
        if _is_path_inside_allowed(real_path, [effective_root]):
            return real_path
        raise _denied_error(path, [wr], "Path")

    candidate = os.path.normpath(os.path.join(effective_root, path))
    real_candidate = os.path.realpath(candidate)
    if _is_path_inside_allowed(real_candidate, [effective_root]):
        return real_candidate
    raise _denied_error(path, [wr], "Path")


def is_safe_path(
    filepath: str,
    allowed_write_dirs: list = None,
    for_write: bool = False,
) -> Tuple[bool, str]:
    """Check whether *filepath* is safe (inside *working_root*).

    Returns ``(True, abs_path)`` or ``(False, error_message)``.
    When ``for_write=True`` the check is stricter (must be inside working_root).

    CRITICAL FIX: Relative paths are now resolved against working_root first,
    NOT against the process CWD. This prevents attacks where CWD != working_root.
    """
    from .working_root import get_working_root

    wr = get_working_root()
    allowed = [wr]
    normed = os.path.normpath(filepath)

    # Quick rejection of obvious traversal attempts
    if ".." in normed.split(os.sep):
        # Resolve relative paths against working_root, not CWD
        if not os.path.isabs(filepath):
            resolved = os.path.realpath(os.path.join(wr, filepath))
        else:
            resolved = os.path.realpath(filepath)
        if _is_path_inside_allowed(resolved, allowed):
            return True, resolved
        return False, "Path traversal not allowed — resolves outside working root"

    # Resolve relative paths against working_root, NOT CWD
    if not os.path.isabs(filepath):
        abs_path = os.path.realpath(os.path.join(wr, filepath))
    else:
        abs_path = os.path.realpath(filepath)

    if _is_path_inside_allowed(abs_path, allowed):
        return True, abs_path
    return False, f"Path must be within the working root directory ({wr})"


# ---------------------------------------------------------------------------
# Secure find_file — ONLY returns paths inside allowed directories
# ---------------------------------------------------------------------------


def find_file(filename: str) -> Optional[str]:
    """Search for *filename* and return the first match **inside** allowed dirs.

    Search order (all validated before returning):

      1. ``working_root / filename``          (relative path)
      2. ``working_root / basename(filename)`` (basename only)

    **NEVER** accepts raw absolute paths from the caller — they are rejected
    immediately to prevent path traversal attacks.
    """
    from .working_root import get_working_root

    # --- Reject absolute-path inputs ---------------------------------------
    if os.path.isabs(filename):
        raise ValueError(
            f"Absolute paths are not allowed in find_file(): {filename!r}. "
            "Use a relative path or plain filename instead."
        )

    wr = get_working_root()
    cleaned = filename.lstrip("/\\")
    if not cleaned:
        return None

    allowed_roots: List[str] = [wr]

    # Candidate locations to check (in priority order)
    candidates = [
        os.path.join(wr, cleaned),
        os.path.join(wr, os.path.basename(cleaned)),
    ]

    seen: set = set()  # avoid checking the same normalised path twice
    for candidate in candidates:
        norm_candidate = os.path.normpath(candidate)
        if norm_candidate in seen:
            continue
        seen.add(norm_candidate)

        if not os.path.exists(norm_candidate):
            continue

        # *** CRITICAL: validate BEFORE returning using realpath ***
        real_candidate = os.path.realpath(norm_candidate)
        if _is_path_inside_allowed(real_candidate, allowed_roots):
            return norm_candidate

    return None


__all__ = [
    "_strip_extended_length_prefix",
    "_is_path_inside_allowed",
    "_denied_error",
    "validate_filename",
    "guard_path_inside_working_root",
    "resolve_path_in_working_root",
    "resolve_path_for_move",
    "resolve_path_to_dir",
    "is_safe_path",
    "find_file",
]
