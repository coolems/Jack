"""Model Resolver Module - Model name resolution and filtering for auth gateway.

MODEL IDENTITY MODEL (2026-08-23 security fix):
    Each entry in allowed_models_folders represents exactly ONE model.
    The folder may contain multiple .gguf files (multi-part), mmproj/projector
    files, tokenizer files, etc., but the FOLDER IS THE MODEL IDENTITY.

    Authorization uses EXACT folder-name matching only:
      - No normalization (no stripping dots/dashes/underscores)
      - No substring matching (bidirectional or otherwise)
      - Case-insensitive equality only (Windows filesystem semantics)

    This eliminates the over-permissive bug where "qwen3" matched
    "qwen38-27b-mtp-q4" via normalized-substring comparison.
"""

import logging
import os
from typing import Dict, List, Optional, Set

logger = logging.getLogger("COOLEMS.AuthGateway")


# ---------------------------------------------------------------------------
# Folder resolution (profile -> allowed model identities)
# ---------------------------------------------------------------------------

def resolve_folders_to_models(folder_paths: List[str]) -> Optional[List[str]]:
    """Resolve allowed_models_folders to canonical model identifiers.

    Each valid folder contributes exactly ONE identifier: its basename.
    A folder may contain multiple .gguf files (multi-part), mmproj, tokenizers,
    etc., but it represents a single model.

    Returns:
        - None if input is None (admin: all models allowed)
        - List of folder basenames (one per valid existing folder)
        - Empty list [] if no folders exist on disk (deny access)

    Raises:
        ValueError if input is not a list or None.
    """
    if folder_paths is None:
        return None  # Admin - all models allowed

    if not isinstance(folder_paths, list):
        raise ValueError(
            f'[AUTH] resolve_folders_to_models: expected list or None, '
            f'got {type(folder_paths).__name__}. Check profiles.json configuration.'
        )

    resolved = []
    seen = set()
    missing_count = 0
    invalid_count = 0

    for folder in folder_paths:
        if not isinstance(folder, str) or not folder.strip():
            logger.warning(
                f'[AUTH] allowed_models_folders contains invalid entry: {folder!r} '
                f'(type={type(folder).__name__}). Skipping.'
            )
            invalid_count += 1
            continue

        abs_folder = os.path.abspath(folder)

        if os.path.isdir(abs_folder):
            folder_name = os.path.basename(abs_folder)
            key = folder_name.lower()
            if key not in seen:
                resolved.append(folder_name)
                seen.add(key)
        else:
            logger.warning(f'[AUTH] Model folder does not exist on disk: {folder}')
            missing_count += 1

    if missing_count > 0 or invalid_count > 0:
        logger.warning(
            f'[AUTH] Folder resolution: {missing_count} missing, '
            f'{invalid_count} invalid. Resolved {len(resolved)} model(s).'
        )

    if not resolved and (missing_count > 0 or invalid_count > 0):
        logger.warning('[AUTH] Model resolution failed - DENYING access due to misconfigured folders.')
        return []

    if not resolved:
        logger.warning('[AUTH] Model folders specified but none exist on disk.')

    return resolved


# ---------------------------------------------------------------------------
# Exact-match authorization (NO normalization, NO substring)
# ---------------------------------------------------------------------------

def _has_path_separator(s: str) -> bool:
    """True if the string contains a path separator (either OS convention)."""
    return os.sep in s or "/" in s


def _extract_model_folder(model_entry) -> Optional[str]:
    """Extract the model folder identity from a model entry.

    Accepts:
      - dict with "folder" key (preferred): {"name": "...", "folder": "qwen36-27b-mtp"}
      - dict whose "name" is a path: derives the parent folder basename
      - plain string that is a path: derives the parent folder basename

    Returns the folder basename, or None when it cannot be determined.
    """
    if isinstance(model_entry, dict):
        folder = model_entry.get("folder")
        if isinstance(folder, str) and folder.strip():
            return os.path.basename(os.path.abspath(folder))
        name = model_entry.get("name", "")
        if isinstance(name, str) and _has_path_separator(name):
            parent = os.path.dirname(name.replace("/", os.sep))
            base = os.path.basename(parent)
            return base or None
        return None

    if isinstance(model_entry, str) and _has_path_separator(model_entry):
        parent = os.path.dirname(model_entry.replace("/", os.sep))
        base = os.path.basename(parent)
        return base or None

    logger.warning(f"[AUTH] Unexpected model entry type {type(model_entry).__name__} - skipping")
    return None


def _extract_model_name(model_entry) -> Optional[str]:
    """Extract the display name string from a model entry (for logging/errors)."""
    if isinstance(model_entry, str):
        return model_entry
    if isinstance(model_entry, dict):
        raw = model_entry.get("name", "")
        if isinstance(raw, str):
            return raw
    return None


def filter_allowed_models(user_info: Dict, all_models: List) -> List:
    """Filter available models based on user's profile.

    Uses EXACT folder-name matching only (case-insensitive equality).
    No normalization, no substring, no fuzzy logic.

    If allowed_models is None -> admin access to ALL models.
    Otherwise a model entry passes if its folder identity exactly matches
    (case-insensitive) one of the entries in user_info["allowed_models"].

    Entries without any determinable folder identity are DENIED for
    non-admin users (fail-closed): we cannot prove which model they refer to.
    """
    allowed = user_info.get("allowed_models")

    if allowed is None:
        return all_models  # Admin - full access

    if not isinstance(allowed, list):
        raise RuntimeError(
            f'[AUTH] filter_allowed_models: allowed_models must be None or List[str], '
            f'got {type(allowed).__name__} for role "{user_info.get("role", "unknown")}".'
        )

    # Exact-match lookup set (lowercase only - no other normalization)
    allowed_set: Set[str] = {a.lower() for a in allowed if isinstance(a, str)}

    filtered = []
    for model_entry in all_models:
        folder_name = _extract_model_folder(model_entry)
        if folder_name is not None and folder_name.lower() in allowed_set:
            filtered.append(model_entry)

    return filtered


def is_model_allowed(user_info: Dict, model_filename: str, known_folders: Optional[Dict[str, str]] = None) -> bool:
    """Check whether a specific model file is allowed for this user.

    Used by model_switch permission checks (direct_ws package, ws_brain_relay).

    Args:
        user_info: Authenticated user info dict with "allowed_models".
        model_filename: The .gguf filename (or path) the client wants to switch to.
        known_folders: Optional mapping {filename_lower: folder_name} for all
                       discovered model files. When the file is found there, its
                       folder identity is compared exactly against the allowed set.

    Returns:
        True if the user is permitted to use this model, False otherwise.
    """
    allowed = user_info.get("allowed_models")

    if allowed is None:
        return True  # Admin - all models allowed

    if not isinstance(allowed, list):
        return False  # Misconfigured -> deny (fail-closed)

    allowed_set: Set[str] = {a.lower() for a in allowed if isinstance(a, str)}

    basename = os.path.basename(model_filename).lower()

    # Primary path: look up which folder this file belongs to
    if known_folders:
        folder_name = known_folders.get(basename)
        if folder_name:
            return folder_name.lower() in allowed_set

    # Fallback (file unknown on disk): exact match of the filename against the
    # allowed set. Since allowed entries are folder basenames, this only passes
    # for degenerate configs where a folder name equals a filename - no substring.
    return basename in allowed_set


def filter_allowed_tools(user_info: Dict, requested_tools: List[str]) -> List[str]:
    """Filter requested tools based on user's profile.

    If allowed_tools is None -> admin access to ALL requested tools.
    Otherwise only keep tools that are in the allowed list (exact match).
    """
    if not requested_tools:
        return []

    allowed = user_info.get("allowed_tools")

    if allowed is None:
        return requested_tools  # Admin - full access

    allowed_set = set(allowed)
    filtered = [t for t in requested_tools if t in allowed_set]

    removed = set(requested_tools) - set(filtered)
    if removed:
        logger.info(f"[AUTH] Role '{user_info.get('role', 'unknown')}' blocked tools: {removed}")

    return filtered
