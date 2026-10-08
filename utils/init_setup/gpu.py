"""GPU detection + VRAM tier table."""

import subprocess


def detect_gpu() -> tuple[str | None, int | None]:
    """Return (gpu_name, vram_mb) or (None, None) when no NVIDIA GPU / nvidia-smi."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            name_part, mem_part = out.stdout.splitlines()[0].split(",")
            return name_part.strip(), int(mem_part.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None, None


# GPU tiers (VRAM in MB). ctx = CONTEXT_WINDOW_TOKENS value to patch into config/config.py.
def tier_for_vram(vram_mb: int | None) -> dict:
    if vram_mb is None:                      # no NVIDIA GPU found
        return {"id": "CPU", "quant": "IQ1_M", "ctx": 32768,
                "note": "no NVIDIA GPU detected - CPU mode (slow), smallest usable model"}
    gb = vram_mb / 1024.0
    if gb >= 28:
        return {"id": "A", "quant": "Q6_K",   "ctx": 181_072, "note": "~181K context"}
    if gb >= 20:
        return {"id": "B", "quant": "Q5_K_S", "ctx": 98_304,  "note": "~96K context"}
    if gb >= 13:
        return {"id": "C", "quant": "Q3_K_XL","ctx": 65_536,  "note": "~64K context"}
    if gb >= 8:
        ctx = 32_768 if vram_mb < 9_000 else 65_536
        return {"id": "D", "quant": "IQ1_M",  "ctx": ctx,     "note": "~64K context (32K under 9 GB)"}
    return {"id": "E", "quant": "IQ1_M",      "ctx": 32_768,  "note": "~32K context - tight VRAM, expect OOM risk on big chats"}
