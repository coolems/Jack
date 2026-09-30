"""Path resolution utilities for llama.cpp server model discovery.

Handles finding the server root directory, resolving model paths through
allowed folders (from profiles.json), and falling back to default locations.

Uses STRICT real-name matching — no fuzzy token guessing or substring hacks.
"""

import os
import glob
import re as _re
import logging
from typing import Optional

logger = logging.getLogger("COOLEMS.Provider.Llama.Server")


def _resolve_server_root() -> str:
    """Resolve the actual server root directory.

    Walks up from this file until it finds a folder containing llama_server/.
    Works whether running from ROOT or CLIENT subfolder.
    Falls back to 4x dirname as last resort.
    """
    my_dir = os.path.dirname(os.path.abspath(__file__))
    current = my_dir
    while True:
        candidate = os.path.join(current, "llama_server")
        if os.path.isdir(candidate):
            return current  # Found it - this is the server root
        parent = os.path.dirname(current)
        if parent == current:  # Hit filesystem root
            break
        current = parent
    # Fallback to original behavior (4x dirname from server.py)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(my_dir))))


def _get_default_models_dir() -> str:
    """Get the default models directory (llama_server/models)."""
    root_dir = _resolve_server_root()
    return os.path.join(root_dir, "llama_server", "models")


# ---------------------------------------------------------------------------
# Normalisation helpers — canonical form used for ALL comparisons
# ---------------------------------------------------------------------------

def _strip_gguf(name: str) -> str:
    """Remove trailing .gguf (case-insensitive)."""
    if name.lower().endswith(".gguf"):
        return name[: -len(".gguf")]
    return name


def _canonical(name: str) -> str:
    """Return a canonical model identifier.

    Lowercased, no extension, underscores normalised to dashes,
    multiple consecutive dashes collapsed to one.
    """
    n = _strip_gguf(name).lower()
    n = n.replace("_", "-")
    # Collapse runs of dashes (e.g. "model--name" → "model-name")
    while "--" in n:
        n = n.replace("--", "-")
    return n.strip("-")


# ---------------------------------------------------------------------------

def _collect_all_gguf(allowed_folders, default_dir):
    """Return list of full paths to .gguf files across allowed + default dirs.

    Filters out mmproj (vision projector) files.
    Order is deterministic: folders in insertion order, filenames sorted.
    """
    from .model_utils import _is_mmproj_file

    seen = set()
    result = []

    for folder in allowed_folders:
        if not os.path.isdir(folder):
            continue
        for fpath in sorted(glob.glob(os.path.join(folder, "*.gguf"))):
            if _is_mmproj_file(os.path.basename(fpath)):
                continue
            real = os.path.realpath(fpath)
            if real not in seen:
                seen.add(real)
                result.append(fpath)

    # Default dir last (only if not already covered)
    if os.path.isdir(default_dir):
        for fpath in sorted(glob.glob(os.path.join(default_dir, "*.gguf"))):
            if _is_mmproj_file(os.path.basename(fpath)):
                continue
            real = os.path.realpath(fpath)
            if real not in seen:
                seen.add(real)
                result.append(fpath)

    return result


def _get_allowed_folders():
    """Return ordered list of allowed_models_folders from all role profiles."""
    try:
        # Try app.keys first (NETWORK_BETA style), fall back to auth_gateway (ALFA style)
        try:
            from app.keys import get_all_roles, get_role_profile
        except ImportError:
            from app.auth_gateway import AuthGateway as _AG
            _ag = _AG()

            def get_all_roles():
                return list(_ag._profiles_db.keys())

            def get_role_profile(r):
                return _ag._profiles_db.get(r)

        folders_ordered = []
        seen = set()
        for role_name in get_all_roles():
            profile = get_role_profile(role_name)
            if profile:
                folders = profile.get("allowed_models_folders", [])
                if isinstance(folders, list):
                    for folder in folders:
                        if folder not in seen and os.path.isdir(folder):
                            seen.add(folder)
                            folders_ordered.append(folder)
        return folders_ordered

    except Exception as e:
        logger.warning("Could not read allowed folders (falling back to default only): %s", e)
        return []


# ---------------------------------------------------------------------------
# Main resolution function — STRICT real-name matching, NO guessing
# ---------------------------------------------------------------------------

def _resolve_model_path(model_filename: str) -> Optional[str]:
    """Resolve a model filename to its full path.

    Matching strategy (strict, no fuzzy guessing):

    1. Already an existing absolute path → return it immediately
    2. Exact case-insensitive basename match across ALL scanned folders
    3. Canonical-name match (case + extension + underscore/dash normalised)
    4. Fallback: first available model **only** when no quant suffix was requested

    When a specific quantisation is part of the request (Q2, Q3, Q4, Q5, Q6 …)
    we NEVER fall back to a different quant — we return None instead.

    Args:
        model_filename: Model name / filename to resolve.

    Returns:
        Full path to the .gguf file, or None if not found.
    """
    # ---- 1. Absolute path that exists? ----
    if os.path.isabs(model_filename) and os.path.exists(model_filename):
        logger.info("Model resolved (absolute path): %s", model_filename)
        return model_filename

    # Normalise the request
    raw_basename = os.path.basename(model_filename)  # keeps .gguf or not
    canon_requested = _canonical(raw_basename)

    # Detect whether a quantisation suffix is present in the request
    has_quant = bool(_re.search(r"q\d+", canon_requested, _re.IGNORECASE))

    # ---- Gather all candidate files ----
    allowed_folders = _get_allowed_folders()
    default_dir = _get_default_models_dir()
    candidates = _collect_all_gguf(allowed_folders, default_dir)

    if not candidates:
        logger.error("No .gguf model files found in any folder")
        return None

    # ---- 2. Exact case-insensitive basename match ----
    for fpath in candidates:
        if os.path.basename(fpath).lower() == raw_basename.lower():
            logger.info("Model resolved (exact filename): %s", fpath)
            return fpath

    # Also try with .gguf appended / stripped variants of the request
    req_with_ext = raw_basename + ".gguf" if not raw_basename.lower().endswith(".gguf") else raw_basename
    for fpath in candidates:
        if os.path.basename(fpath).lower() == req_with_ext.lower():
            logger.info("Model resolved (exact filename with extension): %s", fpath)
            return fpath

    # ---- 3. Canonical name match (case + _/dash normalised) ----
    for fpath in candidates:
        canon_file = _canonical(os.path.basename(fpath))
        if canon_file == canon_requested:
            logger.info("Model resolved (canonical name): %s", fpath)
            return fpath

    # ---- 4. Fallback — only when user did NOT request a specific quant ----
    if has_quant:
        # User asked for a specific quantisation level and we couldn't find it.
        # Return None so the caller knows the model is genuinely unavailable.
        logger.error(
            "Model '%s' (quant requested) not found in any scanned folder",
            model_filename,
        )
        return None

    # No specific quant — pick first available file as fallback
    result = candidates[0]
    logger.info(
        "No exact match for '%s'; using first available model: %s",
        model_filename,
        os.path.basename(result),
    )
    return result


# ---------------------------------------------------------------------------
# Legacy alias kept for backward compat
# ---------------------------------------------------------------------------

def resolve_model_path(model_filename: str) -> Optional[str]:
    """Public alias — same as _resolve_model_path."""
    return _resolve_model_path(model_filename)
