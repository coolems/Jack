"""Model authorization — EXACT model-filename matching (2026-10 fleet).

The fleet runs different models on different machines, and the brain does not hold the
remote models on disk. Profiles therefore list model FILENAMES explicitly:

    "allowed_models": ["Dirk-Qwen3.8-27B-UD-Q6_K.gguf", "gemma-3-27b-Q6_K.gguf"]
    "allowed_models": null        -> all models (admin)

Authorization compares the GGUF basename, case-insensitively, with the trailing .gguf
optional on either side. No folders, no normalization beyond case/extension, no
substring or fuzzy matching - the same fail-closed spirit as the old folder rule.

MODEL IDENTITY: a model is identified by its basename (see app/providers/llama/
model_identity.py). The node's /props.model_path basename, the client's selected model
name and the profile entry must therefore spell the same file name.
"""

import logging
import os
from typing import Dict, List, Optional, Set

logger = logging.getLogger("COOLEMS.AuthGateway")


def _norm(value) -> str:
    """Canonical authorization key: basename, lowercase, no trailing .gguf."""
    if not isinstance(value, str):
        return ""
    base = os.path.basename(value.strip().replace("\\", "/")).lower()
    if base.endswith(".gguf"):
        base = base[: -len(".gguf")]
    return base.strip()


def _entry_name(entry) -> Optional[str]:
    """Extract the model filename from a catalog entry (dict or string)."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        raw = entry.get("name", "")
        return raw if isinstance(raw, str) else None
    return None


def filter_allowed_models(user_info: Dict, all_models: List) -> List:
    """Keep only the models this profile is allowed to use.

    ``allowed_models`` is None -> admin, everything passes.
    Otherwise a model entry passes when its filename matches an allowed entry exactly
    (case-insensitive, .gguf optional). Entries without a determinable name are DENIED
    for non-admins (fail-closed).
    """
    allowed = user_info.get("allowed_models")

    if allowed is None:
        return all_models

    if not isinstance(allowed, list):
        raise RuntimeError(
            f'[AUTH] filter_allowed_models: allowed_models must be None or a list of '
            f'filenames, got {type(allowed).__name__} for role '
            f'"{user_info.get("role", "unknown")}".'
        )

    allowed_set: Set[str] = {_norm(a) for a in allowed if _norm(a)}

    filtered = []
    for entry in all_models:
        name = _entry_name(entry)
        if name and _norm(name) in allowed_set:
            filtered.append(entry)
    return filtered


def is_model_allowed(user_info: Dict, model_filename: str,
                     known_folders: Optional[Dict[str, str]] = None) -> bool:
    """Check whether a specific model is allowed for this user.

    ``known_folders`` is accepted for backward compatibility and IGNORED - model
    identity is the filename now.
    """
    allowed = user_info.get("allowed_models")

    if allowed is None:
        return True  # Admin - all models allowed

    if not isinstance(allowed, list):
        return False  # Misconfigured -> deny (fail-closed)

    key = _norm(model_filename)
    if not key:
        return False
    return key in {_norm(a) for a in allowed if _norm(a)}


def filter_allowed_tools(user_info: Dict, requested_tools: List) -> List:
    """Filter the requested tool list against the profile's allowed_tools."""
    if not requested_tools:
        return []

    allowed = user_info.get("allowed_tools")

    if allowed is None:
        return requested_tools  # Admin - full access

    allowed_set = set(allowed)
    filtered = [t for t in requested_tools if t in allowed_set]

    removed = set(requested_tools) - set(filtered)
    if removed:
        logger.info("[AUTH] Role '%s' blocked tools: %s", user_info.get("role", "unknown"), removed)

    return filtered
