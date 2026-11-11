"""Memory budget for llama-server launches (2026-11 multi-config fleet).

WHY THIS EXISTS
---------------
llama.cpp loads a whole model into RAM/VRAM and never gives it back. Before this module
anything could spawn a llama-server without asking "is there room for it": an admin who
already had Strata loaded and then started a llama node (or started three configs on three
ports) pushed the machine past its limit and the OS killed everything. The check here runs
BEFORE the spawn and refuses with a concrete number instead.

WHAT IT READS (Windows, stdlib only - no psutil dependency in this process)
  * physical RAM: GlobalMemoryStatusEx (ullTotalPhys / ullAvailPhys)
  * VRAM:         nvidia-smi --query-gpu=memory.total,memory.used  (None when no GPU)
Free RAM is read with a short settle delay: right after a process is terminated the OS can
still report the memory as held, so callers that just freed a llama pass settle_ms.

THE MODEL (deliberately conservative)
  weights          = the .gguf file size (mmap'd, so it counts against RAM)
  kv cache         = slots x ctx_per_slot x kv_heads x (key_len + value_len) x cache
                     quant bytes x layers, read from the GGUF header when readable, then
                     CLAMPED to config LLAMA_SERVER_CACHE_RAM - llama.cpp is launched with
                     --cache-ram <that value>, so it never allocates more than the cap even
                     when the header math says the context would need it
  compute buffers  = a flat reserve per slot (llama.cpp allocates compute + KV-extra
                     buffers on top of weights; llama.cpp's own --fit uses the same idea)
  MTP draft        = a second context on top of the SAME weights, so it is charged as
                     extra buffers, not as another model file

llama.cpp's ``--fit on`` still adjusts flags to fit the device, but it cannot rescue a
machine that is already full - that is what this gate is for.
"""

import ctypes
import logging
import os
import subprocess
import time
from typing import Optional

logger = logging.getLogger("COOLEMS.Provider.Llama.Memory")

# KV cache bytes per token per layer when the header says nothing usable.
# llama.cpp's --cache-type-k/v accepts exactly these quantizations.
_CACHE_BYTES_PER_TOKEN = {"f16": 2.0, "f32": 4.0, "bf16": 2.0, "q8_0": 1.0, "iq8_0": 1.0,
                          "q4_0": 0.5, "q4_1": 0.625, "q5_0": 0.625, "q5_1": 0.75}

# llama.cpp compute buffers (per slot): attention + FF scratch. A rough but honest floor.
_COMPUTE_MB_PER_SLOT = 1200.0
# Extra buffers for speculative decoding (MTP draft context on the same weights).
_MTP_EXTRA_MB = 800.0


class MemorySnapshot:
    """What the machine has right now."""

    def __init__(self, total_ram_mb: int, avail_ram_mb: int,
                 gpu_name: Optional[str], vram_total_mb: Optional[int],
                 vram_used_mb: Optional[int], note: str = ""):
        self.total_ram_mb = int(total_ram_mb)
        self.avail_ram_mb = int(avail_ram_mb)
        self.gpu_name = gpu_name
        self.vram_total_mb = vram_total_mb
        self.vram_used_mb = vram_used_mb
        self.note = note

    @property
    def vram_free_mb(self) -> Optional[int]:
        if self.vram_total_mb is None:
            return None
        return max(0, int(self.vram_total_mb) - int(self.vram_used_mb or 0))

    def describe(self) -> str:
        gpu = (f"GPU {self.gpu_name}: {self.vram_used_mb or 0} MB used of "
               f"{self.vram_total_mb} MB ({self.vram_free_mb} MB free)") if self.vram_total_mb else "no GPU detected"
        ram = f"RAM: {self.avail_ram_mb} MB free of {self.total_ram_mb} MB"
        return f"{ram}; {gpu}" + (f" ({self.note})" if self.note else "")


def _global_memory_status() -> tuple[int, int]:
    """(total MB, available MB) of physical RAM via GlobalMemoryStatusEx."""
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        raise OSError("GlobalMemoryStatusEx failed")
    mb = 1024 * 1024
    return int(m.ullTotalPhys // mb), int(m.ullAvailPhys // mb)


def _nvidia_memory() -> tuple[Optional[str], Optional[int], Optional[int]]:
    """(gpu name, total MB, used MB) from nvidia-smi, or (None, None, None)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None, None, None
        # Sum every GPU: llama.cpp can use more than one card, and the budget must
        # account for the whole machine, not just the first line.
        name = None
        total = 0
        used = 0
        for line in out.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            name = parts[0] if name is None else f"{name} + {parts[0]}"
            total += int(parts[1])
            used += int(parts[2])
        if total <= 0:
            return None, None, None
        return name, total, used
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        logger.debug("[MEM] nvidia-smi unavailable: %s", e)
        return None, None, None


def snapshot(settle_ms: int = 0) -> MemorySnapshot:
    """Current RAM + VRAM. Never raises: a machine we cannot read reports zero free.

    ``settle_ms`` waits before reading - use it right after terminating a process so the
    OS has released the memory before we decide whether the next launch fits."""
    if settle_ms > 0:
        time.sleep(min(int(settle_ms), 10000) / 1000.0)
    try:
        total_ram, avail_ram = _global_memory_status()
    except Exception as e:
        # Non-Windows / exotic build: fall back to the total so at least the absolute
        # cap is known, but report almost nothing as free (fail-safe toward refusing).
        logger.warning("[MEM] could not read physical RAM (%s) - treating free RAM as unknown", e)
        total_ram, avail_ram = 0, 0
    gpu_name, vram_total, vram_used = _nvidia_memory()
    return MemorySnapshot(total_ram, avail_ram, gpu_name, vram_total, vram_used)


def estimate_model_memory_mb(model_path: str, slots: int, ctx_per_slot: int,
                             gpu_layers: Optional[int] = None,
                             mtp: bool = False) -> tuple[int, str]:
    """(estimated MB needed, human breakdown) for one llama-server process.

    Reads the GGUF header for layer count, KV head count and per-head key/value lengths
    when the file is readable; falls back to config defaults when it is not. The file size
    is the weights (llama.cpp mmaps the file, so it is real RAM/VRAM pressure either way).

    KV cache = slots x ctx_per_slot x kv_heads x (k_len + v_len) x bytes_per_element(cache
    quant) x layers — the shape llama.cpp actually allocates, which is why a 27B hybrid model
    with 4 KV heads needs far less context memory than its embedding size would suggest.
    """
    from config import (
        LLAMA_SERVER_CACHE_TYPE_K,
        LLAMA_SERVER_CACHE_TYPE_V,
        LLAMA_SERVER_CACHE_RAM,
    )

    slots = max(1, int(slots or 1))
    ctx_per_slot = max(256, int(ctx_per_slot or 0))

    try:
        weights_mb = int(os.path.getsize(model_path)) // (1024 * 1024)
    except OSError:
        weights_mb = 0

    meta = {}
    try:
        from .model_utils import read_gguf_metadata
        meta = read_gguf_metadata(model_path)
    except Exception as e:  # unreadable header - the fallbacks below still give a number
        logger.debug("[MEM] GGUF header unreadable for %s: %s", model_path, e)

    n_layer = int(meta.get("n_layer") or 0)
    n_kv_head = int(meta.get("n_kv_head") or 0)
    head_dim_k = int(meta.get("head_dim_k") or 0)
    head_dim_v = int(meta.get("head_dim_v") or 0)
    n_embd = int(meta.get("n_embd") or 0)

    cache_k = str(LLAMA_SERVER_CACHE_TYPE_K or "f16").lower()
    cache_v = str(LLAMA_SERVER_CACHE_TYPE_V or "f16").lower()
    bpe_k = _CACHE_BYTES_PER_TOKEN.get(cache_k, 2.0)
    bpe_v = _CACHE_BYTES_PER_TOKEN.get(cache_v, 2.0)

    # Per token per layer: kv heads x (key + value) elements. Without a header, fall back
    # to the config KV-cache ceiling (--cache-ram), which is the cap llama.cpp is told to
    # respect - a guess is safer than a number that looks precise but is not.
    kv_elements = n_kv_head * (head_dim_k + head_dim_v) if (n_kv_head and head_dim_k and head_dim_v) else 0
    if kv_elements <= 0 and n_embd > 0:
        kv_elements = n_embd          # MHA-style fallback: assume full embedding per head
        n_kv_head = n_kv_head or 1

    # The cap llama.cpp is actually given: --cache-ram LLAMA_SERVER_CACHE_RAM is a TOTAL KV
    # budget across all slots, so a huge ctx_per_slot cannot make the allocation grow past it
    # (llama.cpp shrinks the usable context instead). Charging the raw header math would
    # refuse launches that really fit - e.g. a 27B at 185k ctx "needs" 46 GB of KV when the
    # server is told to keep 4 GB.
    cache_cap_mb = int(LLAMA_SERVER_CACHE_RAM or 0)

    if kv_elements > 0 and n_layer > 0:
        raw_kv_mb = int(slots * ctx_per_slot * kv_elements * (bpe_k + bpe_v) * n_layer // (1024 * 1024))
        kv_mb = min(raw_kv_mb, cache_cap_mb) if cache_cap_mb > 0 else raw_kv_mb
        kv_note = (f"{slots} slot(s) x {ctx_per_slot} tok x {n_kv_head or 1} kv_head"
                   f" x {kv_elements} elem x layers={n_layer}, k={cache_k} v={cache_v}")
        if kv_mb < raw_kv_mb:
            kv_note += f", capped by --cache-ram {cache_cap_mb} MB (header math: {raw_kv_mb} MB)"
    else:
        kv_mb = cache_cap_mb
        kv_note = f"config LLAMA_SERVER_CACHE_RAM={kv_mb} MB (no usable GGUF header)"

    compute_mb = int(_COMPUTE_MB_PER_SLOT * slots)
    mtp_mb = int(_MTP_EXTRA_MB * slots) if mtp else 0

    total = weights_mb + kv_mb + compute_mb + mtp_mb
    gpu_note = ""
    if gpu_layers is not None and int(gpu_layers) == 0:
        gpu_note = " (CPU-only: weights land in system RAM)"
    breakdown = (
        f"weights {weights_mb} MB + kv {kv_mb} MB ({kv_note}) "
        f"+ buffers {compute_mb} MB" + (f" + MTP {mtp_mb} MB" if mtp else "") + gpu_note
    )
    return total, breakdown


def check_launch(model_path: str, slots: int, ctx_per_slot: int,
                 gpu_layers: Optional[int] = None, mtp: bool = False,
                 reserve_mb: Optional[int] = None) -> tuple[bool, str]:
    """Decide whether one llama-server may be started here.

    Returns ``(ok, message)``. ``message`` is COMPACT either way: on refusal just
    "<model> needs <x> more GB" (the shortfall vs the resource that ran out - RAM or VRAM,
    reserve included); on success a short budget line. ``reserve_mb`` defaults
    to config LLAMA_LAUNCH_RESERVE_MB - the headroom the brain, the OS and any other loaded
    workload (Strata, another llama node) must keep.
    """
    snap = snapshot()
    need_mb, _breakdown = estimate_model_memory_mb(
        model_path, slots, ctx_per_slot, gpu_layers=gpu_layers, mtp=mtp)

    def _shortfall(available_mb: int) -> str:
        """One compact line: how much MORE this machine would need (reserve included)."""
        return (f"{os.path.basename(model_path)} needs "
                f"{(need_mb + reserve_mb - available_mb) / 1024:.1f} more GB")
    if reserve_mb is None:
        try:
            from config import LLAMA_LAUNCH_RESERVE_MB
            reserve_mb = int(LLAMA_LAUNCH_RESERVE_MB or 0)
        except Exception:
            reserve_mb = 0
    reserve_mb = max(0, int(reserve_mb or 0))

    if snap.total_ram_mb <= 0:
        return False, (
            f"cannot read this machine's memory (GlobalMemoryStatusEx unavailable), so "
            f"starting llama-server ({os.path.basename(model_path)}, ~{need_mb} MB) would be "
            f"a guess - refusing to load it."
        )

    # The absolute cap: a model bigger than the whole machine can never run here.
    if need_mb + reserve_mb > snap.total_ram_mb:
        return False, _shortfall(snap.total_ram_mb)

    if snap.vram_total_mb is not None and int(gpu_layers or 0) > 0:
        free_gpu = snap.vram_free_mb or 0
        if need_mb + reserve_mb > free_gpu:
            return False, _shortfall(free_gpu)

    free_ram = snap.avail_ram_mb
    if need_mb + reserve_mb > free_ram:
        return False, _shortfall(free_ram)

    return True, (
        f"budget ok: needs ~{need_mb} MB, {free_ram} MB RAM free"
        + (f", {snap.vram_free_mb} MB VRAM free" if snap.vram_total_mb else "")
        + (f", {reserve_mb} MB reserved" if reserve_mb else "")
    )
