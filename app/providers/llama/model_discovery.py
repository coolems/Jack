"""Model and mmproj file discovery utilities for llama.cpp server.

Handles finding .gguf model files by STRICT real-name matching (no fuzzy guessing)
and locating vision projector (mmproj) files in model directories.
"""

import os
import glob
import logging
from typing import Optional

logger = logging.getLogger("COOLEMS.Provider.Llama.Server")


def _find_model_file(models_dir: str, model_name: str) -> str:
    """Find the appropriate .gguf model file for the configured model name.

    STRICT real-name matching — NO token-based guessing or substring hacks.

    Matching order:
      1. Exact case-insensitive basename match (with/without .gguf extension)
      2. Canonical name match (case + underscore/dash normalised, stripped extension)
      3. NOT FOUND → return empty string (never pick a wrong file!)

    Args:
        models_dir: Directory to search for .gguf files
        model_name: Model name/filename to match against

    Returns:
        Full path to the matched .gguf file, or empty string if not found.
    """
    from .model_utils import _is_mmproj_file

    gguf_files = glob.glob(os.path.join(models_dir, "*.gguf"))
    # Exclude mmproj files
    gguf_files = [f for f in gguf_files if not _is_mmproj_file(os.path.basename(f))]

    if not gguf_files:
        return ""

    raw_basename = os.path.basename(model_name)

    # ---- 1. Exact case-insensitive basename match (strip/append .gguf variants) ----
    candidates_ext = [raw_basename, raw_basename + ".gguf"]
    for cand in candidates_ext:
        cand_lower = cand.lower()
        if cand_lower.endswith(".gguf"):
            # Try matching with extension
            for fpath in gguf_files:
                if os.path.basename(fpath).lower() == cand_lower:
                    logger.info("Model resolved (exact filename): %s", fpath)
                    return fpath

    # ---- 2. Canonical name match (case + _/dash normalised, no extension) ----
    from .path_utils import _canonical
    canon_requested = _canonical(raw_basename)

    for fpath in gguf_files:
        canon_file = _canonical(os.path.basename(fpath))
        if canon_file == canon_requested:
            logger.info("Model resolved (canonical name): %s", fpath)
            return fpath

    # ---- 3. NOT FOUND — never guess wrong ----
    logger.warning(
        "Model '%s' not found in %s (scanned %d files). Returning empty.",
        model_name, models_dir, len(gguf_files),
    )
    return ""


def _find_mmproj_file(model_file: str) -> str:
    """Find the multimodal projector (mmproj) file for vision support.

    Searches in this order:
    1. The model's own folder (for external folder models with bundled mmproj)
    2. Default models directory (llama_server/models/)

    Returns empty string if no mmproj found (server uses --mmproj-auto).

    Args:
        model_file: Full path to the .gguf model file

    Returns:
        Full path to the mmproj file, or empty string if none found.
    """
    from .path_utils import _get_default_models_dir
    from .model_utils import _find_dedicated_mmproj, _find_generic_mmproj

    # Handle both old and new call signatures (backward compat)
    model_dir = os.path.dirname(os.path.abspath(model_file))
    default_models_dir = _get_default_models_dir()

    # Search in the model's own folder first (handles external folders)
    if model_dir and os.path.isdir(model_dir):
        dedicated = _find_dedicated_mmproj(model_dir, model_file)
        if dedicated:
            logger.info(
                f"Using DEDICATED mmproj from model folder for {os.path.basename(model_file)}: "
                f"{os.path.basename(dedicated)} (prefix-matched)"
            )
            return dedicated

        generic = _find_generic_mmproj(model_dir)
        if generic:
            logger.info(
                f"Using GENERIC mmproj from model folder for {os.path.basename(model_file)}: "
                f"{os.path.basename(generic)} (generic fallback)"
            )
            return generic

    # Search in default models directory - ONLY dedicated matches are safe across folders
    dedicated = _find_dedicated_mmproj(default_models_dir, model_file)
    if dedicated:
        logger.info(
            f"Using DEDICATED mmproj from default dir for {os.path.basename(model_file)}: "
            f"{os.path.basename(dedicated)} (prefix-matched)"
        )
        return dedicated

    # Only use generic from default dir if the model IS in the default dir itself
    # This prevents forcing incompatible mmproj (e.g., Qwen mmproj on gemma) onto external models
    if os.path.abspath(model_dir) == os.path.abspath(default_models_dir):
        generic = _find_generic_mmproj(default_models_dir)
        if generic:
            logger.info(
                f"Using GENERIC mmproj from default dir for {os.path.basename(model_file)}: "
                f"{os.path.basename(generic)} (generic fallback)"
            )
            return generic

    # No compatible mmproj found - let llama.cpp auto-detect (--mmproj-auto)
    logger.info(f"No dedicated mmproj file found for {os.path.basename(model_file)}, using --mmproj-auto")
    return ""

