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


# ---------------------------------------------------------------------------
# MTP (Multi-Token Prediction) detection — filename heuristic + file content
# ---------------------------------------------------------------------------

_GGUF_MAGIC = 0x46554747            # 'gguf' little-endian
_GGUF_MTP_PROBE_MAX_BYTES = 64 * 1024 * 1024   # header (metadata + tensor info) is ~11 MB for a 27B model; cap the probe read
_GGUF_MTP_MAX_KV = 100_000          # sanity caps so a corrupt header can never make us spin
_GGUF_MTP_MAX_TENSORS = 1_000_000
_GGUF_MTP_MAX_ARRAY_LEN = 5_000_000

# Result cache: (abspath, mtime_ns, size) -> bool. Model files are big and stable;
# re-parsing the header on every call would be pure waste. Bounded by clearing when
# it grows past a few entries (recompute is cheap compared to loading the model).
_MTP_CONTENT_CACHE = {}


def _gguf_has_mtp_content(path):
    """Inspect a .gguf file's HEADER and report whether MTP weights are baked in.

    Reads only the GGUF header (magic + metadata KV pairs + tensor-info table —
    typically ~10-30 MB even for 27B+ models with full tokenizer arrays), never
    the multi-GB tensor data section. One chunked read keeps this fast on slow /
    network drives where many small reads are painful.

    MTP evidence (either one is sufficient):
      * metadata key '<arch>.nextn_predict_layers' > 0   (e.g. 'qwen35.nextn_predict_layers')
      * any tensor name containing 'nextn' or an mtp segment
        (baked-in heads look like 'blk.64.nextn.eh_proj.weight'; separate draft
         files in bytkim's mtp/ folder are named the same way)

    Args:
        path: Full path to a .gguf file.

    Returns:
        True  - MTP content detected inside the file.
        False - header parsed cleanly and contains no MTP evidence.
        None  - undeterminable (not a parseable GGUF, truncated by the probe cap,
                unreadable). Callers should fall back to their filename heuristic.
    """
    import struct as _struct

    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size < 24:
        return False

    cap = min(size, _GGUF_MTP_PROBE_MAX_BYTES)
    try:
        with open(path, 'rb') as f:
            data = f.read(cap)
    except OSError:
        return None

    n = len(data)

    def u32(o):
        return _struct.unpack_from('<I', data, o)[0]

    def u64(o):
        return _struct.unpack_from('<Q', data, o)[0]

    if n < 24 or u32(0) != _GGUF_MAGIC:
        return None                       # not a GGUF we can parse -> undeterminable

    version = u32(4)                      # v1/v2 and v3 all supported below
    tensor_count = u64(8)
    kv_count = u64(16)
    if version > 3 or tensor_count > _GGUF_MTP_MAX_TENSORS or kv_count > _GGUF_MTP_MAX_KV:
        return None

    off = 24

    def read_str(o):
        """Read a GGUF string (u64 length + bytes). Raises ValueError on truncation."""
        if o + 8 > n:
            raise ValueError('truncated header')
        ln = u64(o)
        o += 8
        if ln > (1 << 26) or o + ln > n:
            raise ValueError('bad string length')
        return data[o:o + ln].decode('utf-8', 'replace'), o + ln

    def skip_value(o, t):
        """Skip a GGUF metadata value of type *t*; returns the new offset."""
        if t in (0, 1, 2, 3, 4, 5, 10, 11):
            sz = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 10: 8, 11: 8}[t]
            if o + sz > n:
                raise ValueError('truncated header')
            return o + sz
        if t == 6 or t == 12:             # float32 / float64
            sz = 4 if t == 6 else 8
            if o + sz > n:
                raise ValueError('truncated header')
            return o + sz
        if t == 7:                        # bool
            if o + 1 > n:
                raise ValueError('truncated header')
            return o + 1
        if t == 8:                        # string
            if o + 8 > n:
                raise ValueError('truncated header')
            ln = u64(o)
            o += 8
            if ln > (1 << 26) or o + ln > n:
                raise ValueError('bad string length')
            return o + ln
        if t == 9:                        # array
            if o + 12 > n:
                raise ValueError('truncated header')
            et = u32(o)
            cnt = u64(o + 4)
            if et not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12):
                raise ValueError('bad array element type')
            if cnt > _GGUF_MTP_MAX_ARRAY_LEN:
                raise ValueError('array too large')
            o += 12
            for _ in range(cnt):
                o = skip_value(o, et)
            return o
        raise ValueError(f'unknown gguf value type {t}')

    nextn_layers = None
    try:
        # ---- metadata KV section ----
        for _ in range(kv_count):
            key, off = read_str(off)
            if off + 4 > n:
                raise ValueError('truncated header')
            t = u32(off)
            off += 4
            if key.endswith('.nextn_predict_layers'):
                # The MTP signal in metadata. Qwen-family GGUFs (arch 'qwen35' etc.)
                # carry '<arch>.nextn_predict_layers'; we match by suffix so any
                # architecture with the same convention is covered.
                if t == 2:
                    nextn_layers = _struct.unpack_from('<H', data, off)[0]
                    off += 2
                elif t == 4:
                    nextn_layers = u32(off)
                    off += 4
                else:
                    off = skip_value(off, t)
            else:
                off = skip_value(off, t)

        # ---- tensor-info section (names only; data offsets are never touched) ----
        # NOTE: GGUF v3 changed n_dims from u64 to u32 — using the wrong width here
        # desynchronizes the whole table (verified against a real 24 GB file).
        ndims_is_u32 = version >= 3
        for _ in range(tensor_count):
            name, off = read_str(off)
            if off + (4 if ndims_is_u32 else 8) > n:
                raise ValueError('truncated header')
            ndims = u32(off) if ndims_is_u32 else u64(off)
            off += 4 if ndims_is_u32 else 8
            if not (1 <= ndims <= 8):
                raise ValueError(f'bad tensor dims {ndims}')
            off += 8 * ndims              # dim values
            if off + 12 > n:
                raise ValueError('truncated header')
            off += 4                      # ggml type
            off += 8                      # data offset (relative to data start)
            low = name.lower()
            if 'nextn' in low or '.mtp.' in low or low.startswith('mtp'):
                return True               # MTP tensors present - no need to read on

    except (ValueError, _struct.error):
        return None                       # truncated/corrupt -> undeterminable

    return bool(nextn_layers) and nextn_layers > 0


def _is_mtp_model(filename_or_path: str) -> bool:
    """Check if a .gguf model has an MTP (Multi-Token Prediction) head.

    Two-stage detection:
      1. FILENAME heuristic (legacy behavior, zero I/O): 'mtp' in the name,
         e.g. 'Qwen3.6-27B-MTP-Q5_K_M.gguf'. Kept first so renamed files and
         non-existent paths behave exactly as before.
      2. FILE CONTENT check (new, 2026-10-31): when the argument is a path to a
         real .gguf on disk whose filename has no 'mtp', parse its header and
         look for baked-in MTP weights ('<arch>.nextn_predict_layers' > 0 or
         tensor names containing 'nextn'/'mtp'). This catches files like
         'Dirk-Qwen3.8-27B-UD-Q6_K.gguf' that carry the MTP head inside without
         saying so in the name — no more renaming required.

    When detected, the server should start with --spec-type draft-mtp params
    to enable speculative decoding for >2x speedup.

    Args:
        filename_or_path: Basename or full path of a .gguf model file.

    Returns:
        True if MTP is detected (by name or by content), False otherwise.
    """
    name = os.path.basename(filename_or_path)
    if 'mtp' in name.lower():
        return True                       # legacy filename heuristic — unchanged

    # Plain basename with no directory component -> we cannot locate the file,
    # so only the (negative) filename result is available.
    if not (os.path.isabs(filename_or_path) or os.sep in filename_or_path):
        return False

    try:
        st = os.stat(filename_or_path)
    except OSError:
        return False                      # not on disk -> filename heuristic only

    key = (os.path.abspath(filename_or_path), st.st_mtime_ns, st.st_size)
    cached = _MTP_CONTENT_CACHE.get(key)
    if cached is None:
        result = _gguf_has_mtp_content(filename_or_path)
        if result is None:
            return False                  # undeterminable -> keep filename answer (False here)
        if len(_MTP_CONTENT_CACHE) > 64:
            _MTP_CONTENT_CACHE.clear()    # entries are cheap to recompute; stay tiny
        _MTP_CONTENT_CACHE[key] = result
        cached = result
    return cached


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
