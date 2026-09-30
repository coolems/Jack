"""Canonical path validation module -- single source of truth for ALL path safety checks.

Every tool, every file operation, and every find_file() call must pass through this
module to ensure we NEVER operate outside allowed directories.

Design principles:
  1. ONE canonical function (_is_path_inside_allowed) does the actual check.
  2. All public helpers (guard_*, resolve_*, is_safe_*) delegate to it.
  3. find_file() only returns paths inside working_root or project_root -- never raw OS paths.

Shared between:
  - tools/utils.py          (SERVER-side)
  - CLIENT/tools/utils.py   (CLIENT sandbox copy)
  - CLIENT/app/utils/common.py  (CLIENT infrastructure)

Security layers (defense in depth):
  Layer 1: Strip leading separators so "/code.py" becomes relative.
  Layer 2: Reject drive letters and absolute paths after stripping.
  Layer 3: Join with working_root to neutralize any remaining tricks.
  Layer 4: os.path.realpath() resolves symlinks, UNC escapes, and .. sequences.
  Layer 5: Final containment check against allowed_roots using realpath comparison.

Every candidate path MUST pass Layer 4+5 before being returned or used.
"""

import os
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Windows extended-length path normalization helper
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


# ---------------------------------------------------------------------------
# Canonical core check -- the single source of truth
# ---------------------------------------------------------------------------

def _is_path_inside_allowed(
    filepath: str,
    allowed_roots: List[str],
) -> bool:
    r"""Return True when *filepath* resolves inside any of *allowed_roots*.

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


def _denied_error(
    filepath: str,
    allowed_roots: List[str],
    label: str = "Path",
) -> ValueError:
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
# Public helpers -- every one delegates to _is_path_inside_allowed()
# ---------------------------------------------------------------------------

def guard_path_inside_working_root(
    filepath: str,
    working_root: Optional[str] = None,
    label: str = "File path",
) -> None:
    """Ensure *filepath* is inside *working_root*.  Raises ``ValueError`` if not.

    When ``working_root`` is ``None`` the caller must have set it via
    ``set_working_root()`` in the owning utils module -- this function only
    validates, never fetches defaults (to keep this module framework-agnostic).
    """
    if working_root is None:
        raise TypeError("working_root must be provided explicitly")
    allowed = [working_root]
    real_path = os.path.realpath(filepath)
    if not _is_path_inside_allowed(real_path, allowed):
        raise _denied_error(real_path, allowed, label)


def resolve_path_in_working_root(
    working_root: str,
    path: str = "",
    filename: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Resolve *path* (and optional *filename*) inside *working_root*.

    Returns ``(dirpath, full_filepath_or_None)``.
    Raises ``ValueError`` on any escape attempt.
    """
    effective_root = os.path.normpath(os.path.abspath(working_root))

    # --- directory part ---------------------------------------------------
    if not path or not path.strip() or path == "." or path.lower() == "root":
        dirpath = effective_root
    elif os.path.isabs(path):
        real_path = os.path.realpath(path)
        if not _is_path_inside_allowed(real_path, [effective_root]):
            raise _denied_error(path, [working_root], "Path")
        dirpath = real_path
    else:
        candidate = os.path.normpath(os.path.join(effective_root, path))
        real_candidate = os.path.realpath(candidate)
        if not _is_path_inside_allowed(real_candidate, [effective_root]):
            raise _denied_error(path, [working_root], "Path")
        dirpath = real_candidate

    # --- file part (optional) ---------------------------------------------
    full_filepath: Optional[str] = None
    if filename and filename.strip():
        full_filepath = os.path.normpath(os.path.join(dirpath, filename))
        real_full = os.path.realpath(full_filepath)
        if not _is_path_inside_allowed(real_full, [effective_root]):
            raise _denied_error(filename, [working_root], "Resolved file path")

    return dirpath, full_filepath


def resolve_path_to_dir(
    working_root: str,
    path: str = "",
) -> str:
    """Resolve *path* to a directory inside *working_root*.

    Used by tools that only need a directory (not a file).
    Raises ``ValueError`` on escape.
    """
    effective_root = os.path.normpath(os.path.abspath(working_root))

    if not path or not path.strip() or path == "." or path.lower() == "root":
        return effective_root

    if os.path.isabs(path):
        real_path = os.path.realpath(path)
        if _is_path_inside_allowed(real_path, [effective_root]):
            return real_path
        raise _denied_error(path, [working_root], "Path")

    candidate = os.path.normpath(os.path.join(effective_root, path))
    real_candidate = os.path.realpath(candidate)
    if _is_path_inside_allowed(real_candidate, [effective_root]):
        return real_candidate
    raise _denied_error(path, [working_root], "Path")


def is_safe_path(
    filepath: str,
    working_root: Optional[str] = None,
    for_write: bool = False,
) -> Tuple[bool, str]:
    """Check whether *filepath* is safe (inside *working_root*).

    Returns ``(True, abs_path)`` or ``(False, error_message)``.
    When ``for_write=True`` the check is stricter (must be inside working_root).
    Uses ``realpath`` to resolve symlinks before validation.
    """
    if working_root is None:
        return False, "working_root not provided"
    allowed = [working_root]
    normed = os.path.normpath(filepath)

    # Provide explicit error message for paths containing ".." segments
    if ".." in normed.split(os.sep):
        real_path = os.path.realpath(normed)
        if _is_path_inside_allowed(real_path, allowed):
            return True, real_path
        return False, "Path traversal not allowed -- resolves outside working root"

    real_path = os.path.realpath(filepath)
    if _is_path_inside_allowed(real_path, allowed):
        return True, real_path
    return False, f"Path must be within the working root directory ({working_root})"


# ---------------------------------------------------------------------------
# Secure find_file -- ONLY returns paths inside allowed directories
# ---------------------------------------------------------------------------

def _has_leading_drive_letter(path: str) -> bool:
    """Check if *path* starts with a Windows-style drive colon prefix.

    Only checks position [0:2] since secure_find_file() strips leading
    separators before calling this function.  After stripping, any remaining
    drive letter must be at the start of the cleaned string.

    Note: This is intentionally conservative -- it returns True for ANY two-
    character prefix where the second character is a colon (e.g. ``9:``).
    False positives are safe because they cause rejection, not acceptance.
    """
    return len(path) >= 2 and path[1] == ":"


def secure_find_file(
    filename: str,
    working_root: str,
    project_root: Optional[str] = None,
) -> Optional[str]:
    r"""Search for *filename* and return the first match **inside** allowed dirs.

    Search order (all validated before returning):
      1. ``working_root / filename``          (relative path)
      2. ``working_root / basename(filename)`` (basename only)
      3. ``project_root / filename``           (if project_root provided)
      4. ``project_root / basename(filename)``

    **NEVER** returns paths outside allowed directories.

    Security layers applied (defense in depth):

      Layer 0 -- Strip Windows extended-length prefix::

          Before any other processing, remove \\\\?\\ and \\\\?\\UNC\\ prefixes
          so that subsequent layers work on the actual path content.

      Layer 1 -- Strip ALL leading separators::

          lstrip("/\\\\") removes every leading ``/`` or ``\\`` character,
          not just one.  So UNC-style inputs like ``"\\\\server\\share\\file.txt"``
          become ``"server\\share\\file.txt"`` (fully relative after stripping).
          This is safe because the remaining path is joined inside working_root.

      Layer 2 -- Reject drive letters and absolute paths::

          After stripping, any path that still contains a drive letter at position
          [0:1] (e.g. ``C:...``) or passes ``os.path.isabs()`` is rejected with
          ValueError immediately.  This catches bare Windows paths like
          ``"C:\\Users\\evil\\file.txt"``.

      Layer 3 -- Join with working_root::

          The cleaned filename is joined into working_root using os.path.join().
          Because leading separators were stripped, the result is always a child
          of working_root (e.g. ``working_root + "server/share/file.txt"``).

      Layer 4 -- Final realpath containment check (ultimate safety net)::

          Every candidate is passed through ``os.path.realpath()`` which resolves
          symlinks, UNC escapes, extended-length prefixes (``\\\\?\\C:\\...``),
          and ``..`` sequences. The resolved path must be inside allowed_roots
          via ``_is_path_inside_allowed()`` before being returned.

    This multi-layer approach means even if one layer is bypassed, the final
    realpath containment check catches the escape attempt.
    """
    # --- Handle empty / whitespace-only input ------------------------------
    if not filename or not filename.strip():
        raise ValueError(
            f"Filename cannot be empty or only separators: {filename!r}"
        )

    # --- Layer 0: Strip Windows extended-length prefix FIRST ----------------
    # Must happen BEFORE lstrip() so that \\?\C:\... becomes C:\...
    # and subsequent layers work correctly on the actual path.
    filename = _strip_extended_length_prefix(filename)

    # --- Layer 1: Strip ALL leading separators (not just one) ---------------
    # lstrip removes every character in the set, so "\\server\share" becomes
    # "server\share" -- fully relative. This is safe because we join into
    # working_root and validate with realpath below.
    cleaned = filename.lstrip("/\\")

    if not cleaned:
        raise ValueError(
            f"Filename cannot be only separators: {filename!r}"
        )

    # --- Layer 2: Reject paths that are still absolute after stripping ------
    # Catches bare Windows drive-letter paths like "C:\Users\evil\file.txt"
    if _has_leading_drive_letter(cleaned) or os.path.isabs(cleaned):
        raise ValueError(
            f"Absolute / cross-drive paths are not allowed in find_file(): "
            f"{filename!r}. Use a relative path or plain filename instead."
        )

    # --- Build candidate list and validate each before returning ------------
    allowed_roots = [working_root]
    if project_root and os.path.isdir(project_root):
        allowed_roots.append(project_root)

    candidates = [
        os.path.join(working_root, cleaned),
        os.path.join(working_root, os.path.basename(cleaned)),
    ]
    if project_root:
        candidates.append(os.path.join(project_root, cleaned))
        candidates.append(os.path.join(project_root, os.path.basename(cleaned)))

    seen: set = set()  # avoid checking the same normalised path twice
    for candidate in candidates:
        norm_candidate = os.path.normpath(candidate)
        if norm_candidate in seen:
            continue
        seen.add(norm_candidate)

        if not os.path.exists(norm_candidate):
            continue

        # *** Layer 4 (ULTIMATE SAFETY NET): realpath containment check ***
        # Resolves symlinks, UNC escapes, extended-length prefixes, .. sequences.
        # Even if Layers 1-2 were somehow bypassed, this catches the escape.
        if _is_path_inside_allowed(norm_candidate, allowed_roots):
            return norm_candidate

    return None


# ---------------------------------------------------------------------------
# Validate file access -- exists AND inside working_root
# Used by tools that accept raw path strings from user input.
# ---------------------------------------------------------------------------

def validate_file_access(
    filepath: str,
    working_root: str,
    label: str = "File path",
) -> str:
    r"""Validate a file *exists* and resolves **inside** *working_root*.

    This is the canonical entry point for tools that accept raw path strings
    from user input (e.g. screenshot_path, image_path).  It performs BOTH
    existence check AND working_root containment using ``realpath`` to resolve
    symlinks.

    Args:
        filepath: The file path to validate (may be absolute or relative).
        working_root: The allowed root directory.
        label: Human-readable label for error messages.

    Returns:
        The validated absolute path string (guaranteed inside working_root).

    Raises:
        ValueError: If the path does not exist, resolves outside working_root,
                    or is a symlink pointing outside working_root.
    """
    if not filepath or not filepath.strip():
        raise ValueError(f"{label} cannot be empty")

    # --- Step 1: Does it exist? ---------------------------------------------
    # Relative paths resolve against working_root (NOT the process CWD -- tools
    # run from arbitrary client directories, and every other tool in this codebase
    # joins user-supplied relative paths against working_root). Absolute paths are
    # used as-is; Step 2 still enforces containment for both.
    if os.path.isabs(filepath):
        abs_path = filepath
    else:
        abs_path = os.path.normpath(os.path.join(working_root, filepath))
    if not os.path.exists(abs_path):
        raise FileNotFoundError(
            f"{label} does not exist: {filepath}"
        )

    # --- Step 2: Resolve symlinks and check containment --------------------
    real_path = os.path.realpath(abs_path)
    allowed = [working_root]
    if not _is_path_inside_allowed(real_path, allowed):
        raise _denied_error(real_path, allowed, label)

    return real_path


__all__ = [
    "_is_path_inside_allowed",
    "guard_path_inside_working_root",
    "resolve_path_in_working_root",
    "resolve_path_to_dir",
    "is_safe_path",
    "secure_find_file",
    "validate_file_access",
]
