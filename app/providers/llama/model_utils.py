"""
Shared utility functions for the llama.cpp provider.

Centralizes common helpers to avoid duplication across modules.
"""
import os
import glob


def _is_mmproj_file(filename: str) -> bool:
    """Check if a .gguf file is an mmproj (multimodal projector) file, not a real model.

    Args:
        filename: The filename to check (e.g. 'Qwen3.6-mmproj-f8.gguf').

    Returns:
        True if the filename contains 'mmproj' (case-insensitive).
    """
    return 'mmproj' in filename.lower()


def normalize_model_name(name: str) -> str:
    """Normalize a model name for comparison.

    Strips the directory path, converts to lowercase, and removes the .gguf extension.
    Useful for comparing model IDs returned by the API against disk filenames.

    Args:
        name: Raw model identifier (filename, path, or API ID).

    Returns:
        Normalized model name without path, lowercase, without .gguf.

    Examples:
        >>> normalize_model_name('/models/Qwen3.6-27B-Q4_K_M.gguf')
        'qwen3.6-27b-q4_k_m'
        >>> normalize_model_name('qwen3.6-27b-q4_k_m')
        'qwen3.6-27b-q4_k_m'
    """
    n = os.path.basename(name).lower()
    if n.endswith('.gguf'):
        n = n[:-5]
    return n


def _is_mtp_model(filename: str) -> bool:
    """Check if a .gguf file is an MTP (Multi-Token Prediction) model.

    MTP models contain 'MTP' in their filename (e.g. 'Qwen3.6-27B-MTP-Q5_K_M.gguf').
    When detected, the server should start with --spec-type draft-mtp params
    to enable speculative decoding for >2x speedup.

    Args:
        filename: The filename to check (e.g. 'Qwen3.6-27B-MTP-Q5_K_M.gguf').

    Returns:
        True if the filename contains 'MTP' (case-insensitive).
    """
    return 'mtp' in filename.lower()


def _get_model_base_prefix(model_filename: str) -> str:
    """Extract the base prefix of a model name for mmproj matching.

    Strips quantization suffixes and extensions to find the core model identifier.
    The quantization suffix is typically the LAST segment after a dot or dash
    that contains patterns like Q4_K_M, Q6_K, Q8_0, F16, etc.

    Examples:
        >>> _get_model_base_prefix('GLM-OCR.Q4_K_M.gguf')
        'GLM-OCR'
        >>> _get_model_base_prefix('Qwen3.6-27B-MTP-Q6_K.gguf')
        'Qwen3.6-27B-MTP'
        >>> _get_model_base_prefix('Qwen3.6-27B-Abliterated-Heretic-Uncensored-Q6_K.gguf')
        'Qwen3.6-27B-Abliterated-Heretic-Uncensored'

    Args:
        model_filename: The .gguf filename.

    Returns:
        Base prefix string for matching against mmproj filenames.
    """
    import re
    name = os.path.basename(model_filename)
    # Remove .gguf extension
    if name.lower().endswith('.gguf'):
        name = name[:-5]

    # Remove quantization suffix patterns from the END of the name
    # Patterns: Q4_K_M, Q6_K, Q8_0, F16, F32, etc.
    # They appear after a dash or dot at the end
    quant_pattern = r'[.-](?:Q\d+_?[_A-Z]?\w*|F(?:16|32))$'
    name = re.sub(quant_pattern, '', name, flags=re.IGNORECASE)

    return name


def _find_dedicated_mmproj(models_dir: str, model_filename: str):
    """Find a dedicated mmproj file that matches the model by prefix.

    Matching logic (PREFIX-based + SUBSTRING fallback):
    1. Extract base prefix from model name (e.g., 'GLM-OCR' from 'GLM-OCR.Q4_K_M.gguf')
    2. Look for an mmproj file whose name contains '<prefix>.mmproj' or starts with it
       - GLM-OCR.Q4_K_M.gguf -> matches: GLM-OCR.mmproj-Q8_0.gguf

    3. SUBSTRING FALLBACK: If no prefix match, check if the model's core tokens
       appear inside the mmproj filename (handles 'mmproj-gemma-4-26B-A4B-it-BF16.gguf')
       - gemma-4-26B-A4B-it-uncensored-heretic-Q6_K -> matches: mmproj-gemma-4-26B-A4B-it-BF16.gguf

    Args:
        models_dir: Path to the models directory.
        model_filename: The .gguf model filename (or full path).

    Returns:
        Full path to matching mmproj file, or None if no dedicated match found.
    """
    base_prefix = _get_model_base_prefix(model_filename)

    # Get all mmproj files
    mmproj_files = [f for f in glob.glob(os.path.join(models_dir, "*.gguf"))
                    if _is_mmproj_file(os.path.basename(f))]

    # --- Method 1: Prefix-based match (e.g., GLM-OCR.mmproj-Q8_0.gguf) ---
    base_lower = base_prefix.lower()
    for mmproj_path in mmproj_files:
        mmproj_name = os.path.basename(mmproj_path).lower()
        if f"{base_lower}.mmproj" in mmproj_name or mmproj_name.startswith(f"{base_lower}.mmproj"):
            return mmproj_path

    # --- Method 2: Substring match (e.g., mmproj-gemma-4-26B-A4B-it-BF16.gguf) ---
    # Split the model prefix into tokens and look for progressively shorter substrings
    # in the mmproj filename. This handles cases where the mmproj has a shorter name
    # than the fine-tuned variant of the model (e.g., "uncensored-heretic" suffix).
    # We require at least 3 matching segments to avoid false positives.
    tokens = base_lower.replace('-', ' ').replace('_', ' ').split()
    if len(tokens) >= 2:
        # Build substrings from longest to shortest (at least 2 tokens joined by -)
        for length in range(len(tokens), 1, -1):
            for start in range(len(tokens) - length + 1):
                substring = '-'.join(tokens[start:start + length])
                if len(substring) < 5:
                    continue
                for mmproj_path in mmproj_files:
                    mmproj_name = os.path.basename(mmproj_path).lower()
                    # The substring should appear as a word boundary match (not partial token)
                    if substring in mmproj_name:
                        return mmproj_path

    return None


def _find_generic_mmproj(models_dir: str) -> str:
    """Find a generic (non-dedicated) mmproj file for vision support.

    Generic mmproj files are ones that DON'T match any specific model prefix.
    They typically have names like 'mmproj-F16.gguf', 'mmproj-F32.gguf', etc.
    These start with 'mmproj-' or 'mmproj.' directly (no model name prefix).

    Priority order:
    1. mmproj-F16.gguf (best quality)
    2. mmproj-F32.gguf (highest quality, largest)
    3. Any other generic mmproj file

    Args:
        models_dir: Path to the models directory.

    Returns:
        Full path to generic mmproj file, or empty string if none found.
    """
    mmproj_files = [f for f in glob.glob(os.path.join(models_dir, "*.gguf"))
                    if _is_mmproj_file(os.path.basename(f))]

    # Build lookup by basename (lowercase)
    mmproj_by_name = {os.path.basename(f).lower(): f for f in mmproj_files}

    # Filter to truly generic mmproj files only - ones that start with 'mmproj-' or 'mmproj.'
    # AND do NOT contain a model name after the prefix (no recognizable model identifier)
    generic_candidates = []
    for name_lower, path in mmproj_by_name.items():
        if not (name_lower.startswith('mmproj-') or name_lower.startswith('mmproj.')):
            continue
        # A truly generic mmproj looks like: mmproj-F16.gguf, mmproj-f32.gguf, etc.
        # It should NOT contain model family names like 'gemma', 'qwen', 'glm', 'llama' after the prefix
        remainder = name_lower.replace('mmproj-', '', 1).replace('mmproj.', '', 1)
        # If remainder only contains quantization patterns (F16, F32, Q8_0, etc.) and extensions -> generic
        import re
        if re.match(r'^(f(?:16|32)|q\d+_?[a-z]?)\.gguf$', remainder):
            generic_candidates.append((name_lower, path))

    # Priority: F16 > F32 > any other
    for priority_name in ['mmproj-f16.gguf', 'mmproj-f32.gguf']:
        if priority_name in mmproj_by_name:
            return mmproj_by_name[priority_name]

    # Fallback to first generic candidate
    if generic_candidates:
        return generic_candidates[0][1]

    return ""


# 2026-08-18: removed dead function _get_disk_ocr_models() — defined but never
# called anywhere (OCR models are resolved from profiles.json in initialization.py).
