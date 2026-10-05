"""Working root management - SINGLE SOURCE OF TRUTH for the whole CLIENT.

CONTRACT (2026-08-19, moved to config/ on 2026-08-27):
  * The ONLY storage for the working root is ``.working_root.json`` inside the
    CLIENT config folder (``<CLIENT>/config/.working_root.json``). It is written by the UI
    ("Working Folder" field) through :func:`set_working_root` and read by
    EVERYTHING: file endpoints, path guards, media handling, tools (tools on
    both SERVER and CLIENT resolve through that same file via tools/utils.py).
  * There is exactly ONE in-memory variable holding the value per process:
    ``_WORKING_ROOT_CACHE``. No other module may keep its own copy of the
    working root - they must call :func:`get_working_root`.
  * If ``.working_root.json`` is missing, empty or corrupt, it is RE-CREATED on
    the spot with the current directory (``os.getcwd()``) as its value and that
    value is used.
  * If the persisted path NO LONGER EXISTS (the whole tree was moved/renamed -
    e.g. ``C:\...\Jack`` became ``C:\...\Jack_bad``), the file is re-pointed at
    THIS tree's own root instead: ``<repo>`` normally, or ``<repo>/CLIENT`` when
    the saved value pointed at the CLIENT subtree itself (2026-10-03 fix - a blind
    CWD fallback made moved installs silently work against the wrong folder until
    someone noticed). A loud warning tells the user to re-select in the UI if they
    meant a different folder. There are NO other fallbacks: nothing else in this
    codebase may substitute a different directory for the working root.

The file lives INSIDE THE CLIENT SUBTREE so the whole tree can be moved to
other machines; SERVER code never hardcodes it - it resolves the same file.
"""

import json
import logging
import os

logger = logging.getLogger("COOLEMS.Tools")


# This module sits at CLIENT/app/utils/common/ -> three levels up reach <CLIENT>.
_CLIENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# The single persistence file - the ONLY source of truth for working_root.
# Lives in the CLIENT config folder (moved from the CLIENT root on 2026-08-27),
# next to its shipped example template .working_root.example.json.
_WORKING_ROOT_FILE = os.path.join(_CLIENT_DIR, "config", ".working_root.json")

# The ONE in-memory variable holding the resolved value (per process).
_WORKING_ROOT_CACHE: str | None = None


def _write_working_root_file(value: str) -> None:
    """Write *value* to ``.working_root.json`` (raises on failure).

    Single writer for the persistence file - used both by :func:`get_working_root`
    when it has to create/repair the file and by :func:`set_working_root`.
    Callers decide whether a failure is fatal.
    """
    with open(_WORKING_ROOT_FILE, "w", encoding="utf-8") as f:
        json.dump({"working_root": value}, f, indent=2)


def _load_persisted_working_root() -> str | None:
    """Read ``.working_root.json`` and return its value when valid.

    Returns the saved absolute path when the file exists, is valid JSON and
    points at a real directory. Returns ``None`` for every other case (file
    missing, empty value, invalid JSON, unreadable file, or saved path no
    longer existing). This function never invents values - callers re-create
    the file in that case.
    """
    try:
        if not os.path.isfile(_WORKING_ROOT_FILE):
            return None
        with open(_WORKING_ROOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_path = (data.get("working_root") or "").strip()
        if not saved_path:
            return None
        resolved = os.path.abspath(saved_path)
        if not os.path.isdir(resolved):
            logger.warning(
                "Persisted working root does not exist: %s - re-creating .working_root.json",
                resolved,
            )
            return None
        return resolved
    except Exception as e:
        logger.warning("Failed to read %s (%s) - re-creating it.", _WORKING_ROOT_FILE, e)
        return None



def _read_saved_value() -> str | None:
    """Return the raw persisted value even when the directory no longer exists.

    Unlike :func:`_load_persisted_working_root` (which only accepts existing
    directories), this returns whatever string the file holds - so a VANISHED path
    can be distinguished from a missing/empty/corrupt file and fed to
    :func:`_self_heal_candidate`. Returns ``None`` for every no-usable-value case.
    Never raises.
    """
    try:
        if not os.path.isfile(_WORKING_ROOT_FILE):
            return None
        with open(_WORKING_ROOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_path = (data.get("working_root") or "").strip()
        return saved_path or None
    except Exception:
        return None


def _self_heal_candidate(saved_path: str) -> str | None:
    """Guess where a vanished working root moved to.

    The tree is designed to be relocated (the config file travels with it), so when
    the persisted path no longer exists the most likely cause is that the whole repo
    was renamed/moved on this machine. Re-point at THIS tree's own root instead of
    blindly falling back to CWD:

      * saved value pointed at a repo root   -> ``<repo>`` (parent of CLIENT/)
      * saved value pointed at CLIENT itself (basename 'client') -> ``<repo>/CLIENT``

    Returns an existing directory different from *saved_path*, or ``None`` when no
    sensible candidate exists. Never raises.
    """
    try:
        saved = os.path.normpath(os.path.abspath(saved_path))
        repo_root = os.path.dirname(_CLIENT_DIR)  # <repo> containing this CLIENT/
        if os.path.basename(saved).lower() == "client":
            candidate = os.path.join(repo_root, "CLIENT")
        else:
            candidate = repo_root
        candidate = os.path.normpath(candidate)
        if candidate != saved and os.path.isdir(candidate):
            return candidate
    except Exception:
        pass
    return None


def get_working_root() -> str:
    """Return the current working root (always from ``.working_root.json``).

    Resolution order:
      1. ``_WORKING_ROOT_CACHE`` when it still points at an existing directory.
      2. Re-read ``.working_root.json`` and use its value when valid.
      3. When the saved path VANISHED (tree moved/renamed): re-point the file at
         this tree's own root via :func:`_self_heal_candidate` (loud warning).
      4. Otherwise CREATE/REWRITE the file with the current directory
         (``os.getcwd()``) as its value and use that value.

    The returned path is ALWAYS a real, existing directory - this function never
    substitutes any other folder for the working root without logging it.
    """
    global _WORKING_ROOT_CACHE
    if _WORKING_ROOT_CACHE:
        try:
            if os.path.isdir(_WORKING_ROOT_CACHE):
                return _WORKING_ROOT_CACHE
        except Exception:
            pass

    persisted = _load_persisted_working_root()
    if persisted is not None:
        _WORKING_ROOT_CACHE = persisted
        logger.info("Working root loaded from %s: %s", _WORKING_ROOT_FILE, persisted)
        return _WORKING_ROOT_CACHE

    # FIX (2026-10-03): the saved path vanished - the tree was most likely moved or
    # renamed on this machine. Re-point at THIS tree's own root instead of blindly
    # falling back to CWD, so a relocated install keeps working against its project
    # folder (the old behavior silently pointed tools at whatever CWD happened to be).
    saved_raw = _read_saved_value()
    if saved_raw:
        healed = _self_heal_candidate(saved_raw)
        if healed is not None:
            try:
                _write_working_root_file(healed)
                logger.warning(
                    "Persisted working root %s no longer exists - the tree appears to have "
                    "been moved/renamed. Re-pointed %s at %s. If you meant a different "
                    "folder, re-select it in the UI (Working Folder field).",
                    saved_raw, _WORKING_ROOT_FILE, healed,
                )
            except Exception as e:
                logger.error("Self-heal could not rewrite %s (%s) - using %s for this process.",
                             _WORKING_ROOT_FILE, e, healed)
            _WORKING_ROOT_CACHE = healed
            return healed

    # File missing/invalid -> create it with the current directory (single rule).
    created = os.path.abspath(os.getcwd())
    try:
        _write_working_root_file(created)
        logger.info(
            "Working root file %s was missing or invalid - created it with the current directory: %s",
            _WORKING_ROOT_FILE, created,
        )
    except Exception as e:
        # Writing failed (permissions): we still have to return a valid dir.
        logger.error(
            "Could not create %s (%s) - using the current directory for this process.",
            _WORKING_ROOT_FILE, e,
        )
    _WORKING_ROOT_CACHE = created
    return _WORKING_ROOT_CACHE


def set_working_root(path: str) -> None:
    """Set the working root (UI action) and persist it to ``.working_root.json``.

    This is the ONLY way the value changes at runtime (besides file self-healing).
    No other layer copies or caches a separate value - tools read the same file
    through tools/utils.py, so nothing has to be propagated anywhere else.

    Parameters
    ----------
    path : str
        Directory path (absolute or relative to CWD). Must point at an existing
        directory. Deleting the file manually resets it: on next access the file
        is re-created with the current directory as its value.

    Raises
    ------
    ValueError
        If *path* is empty or does not exist as a directory.
    RuntimeError
        If the value could not be written to disk (explicit user action fails loudly).
    """
    global _WORKING_ROOT_CACHE
    if not path or not str(path).strip():
        raise ValueError("Cannot set working root: empty path")
    resolved = os.path.abspath(str(path))
    if not os.path.isdir(resolved):
        raise ValueError(f"Cannot set working root: directory does not exist: {resolved}")
    try:
        _write_working_root_file(resolved)
    except Exception as e:
        raise RuntimeError(
            f"Failed to persist working root to {_WORKING_ROOT_FILE}: {e}"
        ) from e
    _WORKING_ROOT_CACHE = resolved  # keep the single in-memory variable in sync
    logger.info("Working root set (persisted to %s): %s", _WORKING_ROOT_FILE, resolved)


# ---------------------------------------------------------------------------
# Backward compatibility - allows ``from common import WORKING_ROOT`` style access
# ---------------------------------------------------------------------------

def __getattr__(name):
    if name == "WORKING_ROOT":
        return get_working_root()
    raise AttributeError("module %r has no attribute '%s'" % (__name__, name))
