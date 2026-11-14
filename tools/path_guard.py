"""Canonical path validation module -- single source of truth for ALL path safety checks.

Every tool, every file operation, and every find_file() call must pass through this
module to ensure we NEVER operate outside allowed directories.

Design principles:
  1. ONE canonical function (_is_path_inside_allowed) does the actual check.
  2. All public helpers (guard_*, resolve_*, is_safe_*) delegate to it.
  3. find_file() only returns paths inside working_root or project_root -- never raw OS paths.

Shared between:
  - tools/utils.py                (SERVER-side delegating helpers)
  - CLIENT/app/utils/common/path_security.py
      The CLIENT infrastructure twin. It ships its OWN copy of the canonical check
      (deliberate: the CLIENT process cannot import tools/), so ANY change here must
      be mirrored there -- the two are kept byte-for-byte equivalent in
      _strip_extended_length_prefix / _resolve_root / _cmp_key / _anchor_relative /
      _is_path_inside_allowed, and tests/test_path_guard_case.py parametrizes BOTH.
      The public helpers differ only where the CLIENT resolves its working root
      itself (get_working_root()) instead of taking it as an argument.

Security layers (defense in depth):
  Layer 1: Strip leading separators so "/code.py" becomes relative.
  Layer 2: Reject drive letters and absolute paths after stripping.
  Layer 3: Join with working_root to neutralize any remaining tricks.
  Layer 4: os.path.realpath() resolves symlinks, UNC escapes, and .. sequences.
  Layer 5: Final containment check against allowed_roots -- BOTH sides realpath'd
           and case-folded with os.path.normcase() (Windows is case-insensitive,
           and realpath() returns the on-disk case). See _is_path_inside_allowed().

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
    """
    return _strip_extended_length_prefix(os.path.realpath(root))


def _anchor_relative(filepath: str, root: str) -> str:
    r"""Absolute form of *filepath*, anchored on *root* -- never on the process CWD.

    SECURITY (2026-11-14 review fix A6): ``realpath()`` anchors a relative path on
    ``os.getcwd()``, and tools do NOT run from working_root (the CLIENT process
    starts in its own folder, and python_exec runs code from wherever it likes).
    So a relative name like ``"code.py"`` used to resolve to whatever file of that
    name sits in CWD -- denied when CWD was outside the root, and worse, ACCEPTED
    when a CWD file happened to land inside the root (the wrong file, silently).
    Every other resolver in this module joins relative paths onto the root; this
    helper makes the plain validators do the same.

    Absolute paths are returned unchanged -- containment is enforced afterwards by
    ``_is_path_inside_allowed()``, which is what actually decides access.

    A Windows DRIVE-relative path (``"C:x\\y"``) is also left unchanged: it cannot
    be anchored on the root (it names a different drive's directory), so the
    containment check resolves it against CWD and denies it -- fail-closed.
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


# ---------------------------------------------------------------------------
# Canonical core check -- the single source of truth
# ---------------------------------------------------------------------------

def _is_path_inside_allowed(
    filepath: str,
    allowed_roots: List[str],
) -> bool:
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
    # Relative names are anchored on the root (see _anchor_relative), NOT on CWD.
    real_path = os.path.realpath(_anchor_relative(filepath, working_root))
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

    ``effective_root`` is the REALPATH'd root (real case, symlinks/junctions
    resolved) -- see _resolve_root(). Using abspath() here used to hand back a
    case-different or junction spelling of the root that then failed the
    containment check on its own children.
    """
    effective_root = _resolve_root(working_root)

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

    ``effective_root`` is the REALPATH'd root (real case, symlinks/junctions
    resolved) -- see _resolve_root().
    """
    effective_root = _resolve_root(working_root)

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
    Uses ``realpath`` to resolve symlinks before validation.

    Relative names are anchored on *working_root*, NOT on the process CWD -- see
    ``_anchor_relative()``. This mirrors the CLIENT twin and every other resolver
    in this module (2026-11-14 review fix A6).

    ``for_write=True`` is STRICTER: the target's DIRECTORY must resolve inside
    *working_root* too, so a directory-level write (a target that IS the root
    itself) and any path whose containing directory lands outside the root is
    refused - the containment check alone would let those through.
    """
    if working_root is None:
        return False, "working_root not provided"
    allowed = [working_root]
    normed = os.path.normpath(filepath)

    real_path = os.path.realpath(_anchor_relative(filepath, working_root))

    # Provide explicit error message for paths containing ".." segments
    if ".." in normed.split(os.sep) and not _is_path_inside_allowed(real_path, allowed):
        return False, "Path traversal not allowed -- resolves outside working root"

    if not _is_path_inside_allowed(real_path, allowed):
        return False, f"Path must be within the working root directory ({working_root})"

    if for_write:
        parent = os.path.realpath(os.path.dirname(real_path))
        if not _is_path_inside_allowed(parent, allowed):
            return False, (
                f"Write target's directory ({parent}) is outside the working root "
                f"({working_root})"
            )
    return True, real_path


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
    # Candidates are built on the RESOLVED roots (see _resolve_root), so the
    # returned path is the on-disk spelling of the directory: a root handed in as
    # 'c:\\transfer\\jack' or as a symlink/junction would otherwise come back in
    # that spelling, and a caller comparing it against realpath() output (or
    # writing it back to disk) would see a path that no other resolver produces.
    # Containment is still checked against the roots the caller supplied.
    root_real = _resolve_root(working_root)
    allowed_roots = [root_real]
    project_root_real = None
    if project_root:
        project_root_real = _resolve_root(project_root)
        if os.path.isdir(project_root_real):
            allowed_roots.append(project_root_real)

    candidates = [
        os.path.join(root_real, cleaned),
        os.path.join(root_real, os.path.basename(cleaned)),
    ]
    if project_root_real:
        candidates.append(os.path.join(project_root_real, cleaned))
        candidates.append(os.path.join(project_root_real, os.path.basename(cleaned)))

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
    abs_path = os.path.normpath(_anchor_relative(filepath, working_root))
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
    "_strip_extended_length_prefix",
    "_resolve_root",
    "_cmp_key",
    "_anchor_relative",
    "_is_path_inside_allowed",
    "guard_path_inside_working_root",
    "resolve_path_in_working_root",
    "resolve_path_to_dir",
    "is_safe_path",
    "secure_find_file",
    "validate_file_access",
]
