"""Shared utility functions for the llama.cpp provider.

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
# GGUF header reading - ONE walker for both MTP detection and memory estimates
# ---------------------------------------------------------------------------

_GGUF_MAGIC = 0x46554747            # 'gguf' little-endian
_GGUF_HEADER_PROBE_MAX_BYTES = 64 * 1024 * 1024   # header (metadata + tensor info) is ~11 MB for a 27B model; cap the probe read
_GGUF_MTP_MAX_KV = 100_000          # sanity caps so a corrupt header can never make us spin
_GGUF_MTP_MAX_TENSORS = 1_000_000
_GGUF_MTP_MAX_ARRAY_LEN = 5_000_000

# ggml tensor type -> bytes per element (llama.cpp ggml.h enum order). Used by the
# memory estimator when it wants a per-element size without the file size.
_GGML_TYPE_BYTES = {
    0: 4.0,    # f32
    1: 2.0,    # f16
    2: 2.0,    # q4_0
    3: 2.0,    # q4_1
    4: 2.0,    # q5_0
    5: 2.0,    # q5_1
    6: 2.0,    # q8_0
    7: 1.0,    # q8_1
    8: 4.0,    # bf16
    9: 2.0,    # q2_0
    10: 3.0,   # q2_1
    11: 4.0,   # q5_2 (older builds) / iq4_nl
    12: 4.0,   # f64
    13: 2.0,   # q4_3
    14: 1.0,   # q5_3
    15: 1.0,   # q6_0
    16: 1.0,   # q8_k
}

# Result cache: (abspath, mtime_ns, size) -> parsed header. Model files are big and
# stable; re-parsing the header on every call would be pure waste. Bounded by clearing
# when it grows past a few entries (recompute is cheap compared to loading the model).
_HEADER_CACHE = {}


def _read_gguf_header(path: str, max_bytes: int = _GGUF_HEADER_PROBE_MAX_BYTES) -> dict:
    """Parse a .gguf HEADER (metadata KV + tensor-info table) - never the tensor data.

    Reads only the header section (typically ~10-30 MB even for 27B+ models with full
    tokenizer arrays) in ONE chunked read, so this stays fast on slow / network drives
    where many small reads are painful.

    Returns a dict:
      version, tensor_count, kv_count      : header counters
      meta    : {key: scalar} for every scalar metadata value (strings included)
      arrays  : {key: [scalars]} for array metadata (strings inside arrays are skipped)
      types   : {ggml_type: count}     tensor-info types (dominant type = the quant)
      names   : [tensor names]         only when the walk survived the whole table
      mtp     : True when '<arch>.nextn_predict_layers' > 0 or a 'nextn'/'.mtp.' tensor exists
      truncated / error : set when the header could not be fully read ('' when clean)

    Returns {} when the file is missing, unreadable or not a parseable GGUF. Never raises.
    """
    import struct as _struct

    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    if size < 24:
        return {}

    try:
        mtime_ns = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    key = (os.path.abspath(path), mtime_ns, size)
    cached = _HEADER_CACHE.get(key)
    if cached is not None:
        return cached

    cap = min(size, max_bytes)
    try:
        with open(path, 'rb') as f:
            data = f.read(cap)
    except OSError:
        return {}

    n = len(data)

    def u32(o):
        return _struct.unpack_from('<I', data, o)[0]

    def u64(o):
        return _struct.unpack_from('<Q', data, o)[0]

    if n < 24 or u32(0) != _GGUF_MAGIC:
        return {}                       # not a GGUF we can parse

    version = u32(4)                    # v1/v2 and v3 all supported below
    tensor_count = u64(8)
    kv_count = u64(16)
    if version > 3 or tensor_count > _GGUF_MTP_MAX_TENSORS or kv_count > _GGUF_MTP_MAX_KV:
        return {}

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

    def read_scalar(o, t):
        """Read one scalar metadata value; returns (value, new_offset)."""
        fmt = {0: '<B', 1: '<B', 2: '<H', 3: '<H', 4: '<I', 5: '<i', 6: '<f', 7: '<?',
               10: '<Q', 11: '<q', 12: '<d'}
        if t not in fmt:
            raise ValueError(f'unknown gguf value type {t}')
        sz = _struct.calcsize(fmt[t])
        if o + sz > n:
            raise ValueError('truncated header')
        return _struct.unpack_from(fmt[t], data, o)[0], o + sz

    def skip_value(o, t):
        """Skip a metadata value of type *t*; returns the new offset."""
        if t == 8:                        # string
            _s, o = read_str(o)
            return o
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
        _v, o = read_scalar(o, t)
        return o

    result = {
        "version": version, "tensor_count": tensor_count, "kv_count": kv_count,
        "meta": {}, "arrays": {}, "types": {}, "names": [], "mtp": False,
        "truncated": "", "error": "",
    }

    try:
        # ---- metadata KV section ----
        for _ in range(kv_count):
            key_name, off = read_str(off)
            if off + 4 > n:
                raise ValueError('truncated header')
            t = u32(off)
            off += 4
            if t == 8:
                result["meta"][key_name], off = read_str(off)
                continue
            if t == 9:
                et = u32(off)
                cnt = u64(off + 4)
                if et not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12) or cnt > _GGUF_MTP_MAX_ARRAY_LEN:
                    raise ValueError('bad array')
                vals = []
                off += 12
                for _ in range(cnt):
                    if et == 8:
                        vals.append(None)      # string elements: not needed by any caller
                        off = read_str(off)[1]
                    else:
                        v, off = read_scalar(off, et)
                        vals.append(v)
                result["arrays"][key_name] = vals
                continue
            result["meta"][key_name], off = read_scalar(off, t)

        # ---- tensor-info section (names + types only; data offsets never touched) ----
        # GGUF tensor info layout: name(string), n_dims(u32 - u64 in v2), dims
        # (u64 x n_dims), type(u32), offset(u64). Only n_dims changed width in v3;
        # the dim VALUES stay u64 (verified against a real 24 GB v3 file - reading them
        # as u32 desynchronizes the whole table and returns an empty/ truncated walk).
        ndims_is_u32 = version >= 3
        names = []
        types: dict = {}
        for _ in range(tensor_count):
            name, off = read_str(off)
            if off + (4 if ndims_is_u32 else 8) > n:
                raise ValueError('truncated header')
            ndims = u32(off) if ndims_is_u32 else u64(off)
            off += 4 if ndims_is_u32 else 8
            if not (1 <= ndims <= 8):
                raise ValueError(f'bad tensor dims {ndims}')
            off += 8 * ndims              # dim values (u64 each)
            if off + 12 > n:
                raise ValueError('truncated header')
            gtype = u32(off)
            off += 4                      # ggml type
            off += 8                      # data offset (relative to data start)
            types[gtype] = types.get(gtype, 0) + 1
            low = name.lower()
            if 'nextn' in low or '.mtp.' in low or low.startswith('mtp'):
                result["mtp"] = True
            names.append(name)

        result["types"] = types
        result["names"] = names
    except (ValueError, _struct.error) as e:
        # Truncated/corrupt: keep whatever we read before the failure so callers can
        # still use metadata (n_layer/n_embd arrive before the tensor table).
        result["truncated"] = str(e)

    # MTP signal from metadata (baked-in heads also show as 'nextn' tensors above).
    for k, v in result["meta"].items():
        if k.endswith('.nextn_predict_layers') and isinstance(v, int) and v > 0:
            result["mtp"] = True

    if len(_HEADER_CACHE) > 64:
        _HEADER_CACHE.clear()    # entries are cheap to recompute; stay tiny
    _HEADER_CACHE[key] = result
    return result


def read_gguf_metadata(path: str) -> dict:
    """The fields a memory estimate needs from a .gguf header.

    (2026-11 memory budget) Returns a dict with:
      n_layer (int), n_embd (int), n_kv_head (int), head_dim_k (int), head_dim_v (int),
      quant (str, dominant tensor type label), bytes_per_element (float),
      mtp (bool), truncated (str, '' when the whole header was readable).

    Real GGUFs name these per-architecture ('qwen35.block_count', 'llama.embedding_length',
    'new.n_layer'...), so every key is looked up by its SUFFIX across the whole metadata
    map - a suffix match is architecture-agnostic and never guesses a value. Empty dict
    when the file is missing or not a parseable GGUF. Never raises.
    """
    header = _read_gguf_header(path)
    if not header:
        return {}

    meta = header.get("meta") or {}
    types = header.get("types") or {}
    dominant = max(types, key=lambda k: types[k]) if types else None

    def _int(*suffixes) -> int:
        """First positive int found under any key ending with one of *suffixes*."""
        for suffix in suffixes:
            for k, val in meta.items():
                if k.endswith("." + suffix) and isinstance(val, int) and not isinstance(val, bool) and val > 0:
                    return int(val)
        return 0

    return {
        # 'block_count' is the modern spelling of n_layer; 'new.n_layer' the newest one.
        "n_layer": _int("block_count", "n_layer"),
        "n_embd": _int("embedding_length", "n_embd"),
        "n_kv_head": _int("attention.head_count_kv", "n_kv_head"),
        # Per-head key/value lengths: the KV cache is kv_head x (k_len + v_len) elements
        # per token per layer - far more precise than assuming the full embedding size.
        "head_dim_k": _int("attention.key_length", "key_length"),
        "head_dim_v": _int("attention.value_length", "value_length"),
        "mtp": bool(header.get("mtp")),
        "truncated": header.get("truncated") or "",
        "quant": f"ggml_type_{dominant}" if dominant is not None else "",
        "bytes_per_element": _GGML_TYPE_BYTES.get(dominant, 4.0) if dominant is not None else 4.0,
    }


# ---------------------------------------------------------------------------
# MTP (Multi-Token Prediction) detection — filename heuristic + file content
# ---------------------------------------------------------------------------

def _gguf_has_mtp_content(path):
    """Inspect a .gguf file's HEADER and report whether MTP weights are baked in.

    Two evidence sources, either one is sufficient (see :func:`_read_gguf_header`):
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
    header = _read_gguf_header(path)
    if not header:
        return None
    if header.get("truncated"):
        # A partial walk may have missed the tensor table - the metadata answer is
        # still trustworthy, but "no evidence" is NOT a proof of absence.
        return bool(header.get("mtp")) or None
    return bool(header.get("mtp"))


def _is_mtp_model(filename_or_path: str) -> bool:
    """Check if a .gguf model has an MTP (Multi-Token Prediction) head.

    Two-stage detection:
      1. FILENAME heuristic (legacy behavior, zero I/O): 'mtp' in the name,
         e.g. 'Qwen3.6-27B-MTP-Q5_K_M.gguf'. Kept first so renamed files and
         non-existent paths behave exactly as before.
      2. FILE CONTENT check: when the argument is a path to a real .gguf on disk
         whose filename has no 'mtp', parse its header and look for baked-in MTP
         weights ('<arch>.nextn_predict_layers' > 0 or tensor names containing
         'nextn'/'mtp'). This catches files like 'Dirk-Qwen3.8-27B-UD-Q6_K.gguf'
         that carry the MTP head inside without saying so in the name - no more
         renaming required.

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

    content = _gguf_has_mtp_content(filename_or_path)
    if content is None:
        return False                      # undeterminable -> keep filename answer (False here)
    return content


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
        model_filename: The .gguf filename (or full path).

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
