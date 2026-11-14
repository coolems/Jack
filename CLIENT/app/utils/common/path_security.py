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


def _resolve_root(root: str) -> str:
    r"""On-disk form of an allowed root, with its real case preserved.

    ``realpath()`` and NOT ``abspath()``: abspath leaves a root that is a
    symlink/junction unresolved, while the path side of every containment check
    IS realpath'd -- so a junction working_root would deny files genuinely inside
    it. The \\?\ prefix is stripped for the same reason as
    ``_strip_extended_length_prefix()``.

    Case is deliberately NOT folded here: this value is returned to callers as a
    usable path. Case folding happens only inside the comparison in
    ``_is_path_inside_allowed()`` (see ``_cmp_key``).

    SYNC NOTE: identical to tools/path_guard.py::_resolve_root -- both modules
    implement the same canonical containment semantics.
    """
    return _strip_extended_length_prefix(os.path.realpath(root))


def _anchor_relative(filepath: str, root: str) -> str:
    r"""Absolute form of *filepath*, anchored on *root* -- never on the process CWD.

    SECURITY (2026-11-14 review fix A6): ``realpath()`` anchors a relative path on
    ``os.getcwd()``, and the CLIENT process does not run from working_root. A
    relative name like ``"code.py"`` therefore resolved to whatever file of that
    name sits in CWD -- denied when CWD was outside the root, and worse, ACCEPTED
    when a CWD file happened to land inside the root (the wrong file, silently).
    Absolute paths are returned unchanged; containment is enforced afterwards by
    ``_is_path_inside_allowed()``, which is what actually decides access.

    A Windows DRIVE-relative path (``"C:x\\y"``) is also left unchanged: it cannot
    be anchored on the root (it names a different drive's directory), so the
    containment check resolves it against CWD and denies it -- fail-closed.

    SYNC NOTE: identical to tools/path_guard.py::_anchor_relative.
    """
    if os.path.isabs(filepath):
        return filepath
    if os.name == "nt" and len(filepath) >= 2 and filepath[1] == ":":
        return filepath
    return os.path.join(_resolve_root(root), filepath)


def _cmp_key(path: str) -> str:
    r"""Comparable form of a resolved path: ``normcase`` of a realpath'd string.

    SECURITY (2026-11-14 review fix A5): BOTH sides of the containment check go
    through this. Windows is case-insensitive, and ``realpath()`` normalizes a
    path to the case stored on disk -- so a root handed in as 'c:\\transfer\\jack'
    (UI text, config, a lowercased DB value) used to be compared with ``abspath()``
    against a realpath'd file and DENIED files that were genuinely inside the root.
    ``os.path.normcase`` folds case on Windows and is a no-op on POSIX.
    """
    return os.path.normcase(path)


def _is_path_inside_allowed(filepath: str, allowed_roots: List[str]) -> bool:
    r"""Return True when *filepath* resolves inside any of *allowed_roots*.

    Normalises BOTH sides with ``os.path.realpath()`` so that symlinks and
    ``..`` tricks are fully resolved before comparison, then folds case with
    ``os.path.normcase()`` so a case-different spelling of the same root cannot
    deny a legitimate path. Using ``realpath`` instead of ``abspath`` on both
    sides prevents symlink-based escape attacks where a link inside working_root
    points to an arbitrary location on disk.

    This is the **ultimate safety net** -- all public functions delegate here
    as their final validation step. Even if earlier checks are bypassed, this
    layer catches: UNC paths (\\\\server\\share), extended-length paths
    (\\\\?\\C:\\...), symlink escapes, and .. traversal.
    """
    # REALPATH resolves symlinks -- abspath does not.
    real_path = _cmp_key(_strip_extended_length_prefix(os.path.realpath(filepath)))

    for root in allowed_roots:
        norm_root = _cmp_key(_resolve_root(root))
        # Separator-anchored prefix: prevents prefix attacks like root + "Evil".
        # rstrip() so a drive root ('C:\') does not become 'C:\\' + sep.
        prefix = norm_root.rstrip(os.sep) + os.sep
        if real_path == norm_root or real_path.startswith(prefix):
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
    """Ensure *filepath* is inside *working_root*.  Raises ``ValueError`` if not.

    Relative names are anchored on the root, NOT on the process CWD -- see
    ``_anchor_relative()`` (2026-11-14 review fix A6).
    """
    from .working_root import get_working_root

    wr = working_root or get_working_root()
    allowed = [wr]
    real_path = os.path.realpath(_anchor_relative(filepath, wr))
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

    ``effective_root`` is the REALPATH'd root (real case, symlinks/junctions
    resolved) -- see _resolve_root(). Using abspath() here used to hand back a
    case-different or junction spelling of the root that then failed the
    containment check on its own children.
    """
    from .working_root import get_working_root

    wr = working_root or get_working_root()
    effective_root = _resolve_root(wr)

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
    """Resolve a move/rename target inside *working_root*.

    Same resolution as ``resolve_path_in_working_root``; *label* only names the
    denial message (e.g. "Source path '...' is outside..."), so a failed move says
    which side of the move was rejected.
    """
    try:
        return resolve_path_in_working_root(working_root, path, filename)
    except ValueError as exc:
        message = str(exc)
        for prefix in ("Path '", "Resolved file path '"):
            if message.startswith(prefix):
                message = message.replace(prefix, f"{label} '", 1)
                break
        raise ValueError(message) from None


def resolve_path_to_dir(
    working_root: Optional[str] = None,
    path: str = "",
) -> str:
    """Resolve *path* to a directory inside *working_root*.

    Used by tools that only need a directory (not a file).
    Raises ``ValueError`` on escape.

    ``effective_root`` is the REALPATH'd root (real case, symlinks/junctions
    resolved) -- see _resolve_root().
    """
    from .working_root import get_working_root

    wr = working_root or get_working_root()
    effective_root = _resolve_root(wr)

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

    ``allowed_write_dirs``: extra roots accepted besides working_root
    (default: working_root only).
    ``for_write=True`` is STRICTER: the target's DIRECTORY must resolve inside an
    allowed root too, so a directory-level write (a target that IS a root itself)
    and any path whose containing directory lands outside every allowed root is
    refused - the containment check alone would let those through.

    CRITICAL FIX: Relative paths are resolved against working_root, NOT the
    process CWD -- see ``_anchor_relative()`` (2026-11-14 review fix A6 mirrors
    this on the SERVER twin).
    """
    from .working_root import get_working_root

    wr = get_working_root()
    allowed = [wr, *(allowed_write_dirs or [])]
    normed = os.path.normpath(filepath)

    # Resolve relative paths against working_root, NOT CWD
    abs_path = os.path.realpath(_anchor_relative(filepath, wr))

    # Quick rejection of obvious traversal attempts
    if ".." in normed.split(os.sep) and not _is_path_inside_allowed(abs_path, allowed):
        return False, "Path traversal not allowed — resolves outside working root"

    if not _is_path_inside_allowed(abs_path, allowed):
        return False, f"Path must be within the working root directory ({wr})"

    if for_write:
        parent = os.path.realpath(os.path.dirname(abs_path))
        if not _is_path_inside_allowed(parent, allowed):
            return False, (
                f"Write target's directory ({parent}) is outside the allowed roots "
                f"({allowed})"
            )
    return True, abs_path


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

    # Candidates are built on the RESOLVED root (see _resolve_root), so the
    # returned path is the on-disk spelling of the directory — a case-different
    # or junction spelling of the working root would otherwise come back in that
    # spelling and never match what the other resolvers produce.
    root_real = _resolve_root(wr)
    allowed_roots: List[str] = [root_real]

    # Candidate locations to check (in priority order)
    candidates = [
        os.path.join(root_real, cleaned),
        os.path.join(root_real, os.path.basename(cleaned)),
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
    "_resolve_root",
    "_cmp_key",
    "_anchor_relative",
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
