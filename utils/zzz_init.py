#!/usr/bin/env python3
"""ZZZ initial init - makes a FRESH GITHUB CLONE of this repo fully runnable on THIS machine.

Entry point: ZZZ_initial_init.bat (repo root) checks for Python >= 3.10 and hands off here.
This script is STDLIB-ONLY (no pip packages, no venv needed) so it runs before anything else exists.

What it does (all steps in one run):
  1. Detects your NVIDIA GPU (nvidia-smi) and recommends a model tier for its VRAM.
  2. Lets you pick the Qwen3.8-27B GGUF quant from unsloth/HuggingFace, then asks whether
     to download it physically NOW or SKIP it for later - everything else is configured
     either way (resumable downloads with progress + size verification; re-run init any
     time to fetch a skipped model).
  3. Optional MTP: renames the main model so "MTP" is in its filename - that is what our
      code (_is_mtp_model) looks for to auto-enable speculative decoding (>2x speedup).
      The MTP head itself is ALREADY baked into unsloth's GGUF, so nothing extra is
      downloaded or loaded (llama.cpp builds the draft context from the same file).
  4. Optional GLM-OCR model from HuggingFace (needed by the transcribe_image tool).
  5. llama.cpp binaries: auto-download of the PINNED build this codebase is written
     against (b10441) - or you can download a newer one by hand and we wait for it.
  6. Config wiring (the server<->client sync part): generates an API key that lands in
     BOTH config/.api_keys.json AND CLIENT/config/.api_client_keys.json, rewrites all
     profile model/OCR paths to THIS machine's real folders, pre-seeds the last-loaded
     model so SERVER boots straight into it, and patches CONTEXT_WINDOW_TOKENS to a
     value your GPU can actually hold.

No venv work (ZZZ_SERVER.bat / ZZZ_CLIENT.bat self-bootstrap on first run) and no smoke
test - init ends with a plain report.

Usage:  python utils\\zzz_init.py        (or just double-click ZZZ_initial_init.bat)
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

# ---------------------------------------------------------------------------
# Constants - verified 2026-09-19 against HuggingFace + GitHub APIs
# ---------------------------------------------------------------------------

MIN_PYTHON = (3, 10)

HF_MODEL_REPO = "unsloth/Qwen3.8-27B-GGUF"
HF_OCR_REPO = "ggml-org/GLM-OCR-GGUF"

LLAMA_BUILD = "b10441"   # PINNED build this codebase is written against (see llama_server/ docs)
GH_RELEASE_BASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}"
# NOTE (verified 2026-09-19 via the GitHub releases API): the Windows CUDA asset is
# versioned per CUDA toolkit - b10441 ships "cuda-12.4", NOT a bare "cuda" name (that one 404s).
LLAMA_ZIP_CUDA = f"{GH_RELEASE_BASE}/llama-b10441-bin-win-cuda-12.4-x64.zip"
LLAMA_ZIP_CUDA_SIZE = 250_792_863
LLAMA_ZIP_CPU = f"{GH_RELEASE_BASE}/llama-b10441-bin-win-cpu-x64.zip"
LLAMA_ZIP_CPU_SIZE = 18_477_134
CUDART_ZIP_124 = f"{GH_RELEASE_BASE}/cudart-llama-bin-win-cuda-12.4-x64.zip"
CUDART_ZIP_SIZE = 391_443_627

# (quant label, exact HF filename, size in bytes) - sizes verified via HF tree API.
QUANTS: list[tuple[str, str, int]] = [
    # NOTE: IQ1_S is NOT offered - it is not smart enough to drive our agentic code.
    ("IQ1_M",   "Qwen3.8-27B-UD-IQ1_M.gguf",    6_729_166_848),
    ("IQ2_XXS", "Qwen3.8-27B-UD-IQ2_XXS.gguf",  7_266_070_528),
    ("IQ2_S",   "Qwen3.8-27B-UD-IQ2_S.gguf",    8_371_970_048),
    ("Q2_K_XL", "Qwen3.8-27B-UD-Q2_K_XL.gguf",  9_828_981_664),
    ("IQ3_XXS", "Qwen3.8-27B-UD-IQ3_XXS.gguf", 10_934_860_704),
    ("IQ3_S",   "Qwen3.8-27B-UD-IQ3_S.gguf",   12_040_883_104),
    ("Q3_K_XL", "Qwen3.8-27B-UD-Q3_K_XL.gguf", 13_146_393_504),
    ("IQ4_XS",  "Qwen3.8-27B-UD-IQ4_XS.gguf",  14_252_845_984),
    ("Q4_K_S",  "Qwen3.8-27B-UD-Q4_K_S.gguf",  15_358_213_024),
    ("Q4_K_M",  "Qwen3.8-27B-UD-Q4_K_M.gguf",  16_464_440_224),
    ("Q4_0",    "Qwen3.8-27B-Q4_0.gguf",       16_056_478_688),
    ("Q4_K_XL", "Qwen3.8-27B-UD-Q4_K_XL.gguf", 17_559_178_144),
    ("Q4_1",    "Qwen3.8-27B-Q4_1.gguf",       17_540_705_248),
    ("Q5_K_S",  "Qwen3.8-27B-UD-Q5_K_S.gguf",  18_665_753_504),
    ("Q5_K_M",  "Qwen3.8-27B-UD-Q5_K_M.gguf",  19_771_509_664),
    ("Q5_K_XL", "Qwen3.8-27B-UD-Q5_K_XL.gguf", 20_876_938_144),
    ("Q6_K",    "Qwen3.8-27B-UD-Q6_K.gguf",    21_983_677_344),
    ("Q6_K_M",  "Qwen3.8-27B-UD-Q6_K_M.gguf",  23_088_409_504),
    ("Q6_K_L",  "Qwen3.8-27B-UD-Q6_K_L.gguf",  24_193_919_904),
    ("Q8_0",    "Qwen3.8-27B-Q8_0.gguf",       29_047_086_048),
]
OCR_FILES: list[tuple[str, int]] = [
    ("GLM-OCR-Q8_0.gguf",        950_433_408),
    ("mmproj-GLM-OCR-Q8_0.gguf", 484_403_648),
]

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


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

class InitError(Exception):
    """Fatal, user-actionable init problem."""


def _utf8_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def line(char: str = "-", width: int = 74) -> str:
    return char * width


def header(title: str) -> None:
    print()
    print(line("="))
    print(f"  {title}")
    print(line("="))


def info(msg: str) -> None:
    print(f"  [i] {msg}")


def ok(msg: str) -> None:
    print(f"  [+] {msg}")


def warn(msg: str) -> None:
    print(f"  [!] {msg}")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def ask(question: str, default: str = "") -> str:
    """Ask a free-text question; empty input returns *default*."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {question}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def ask_yn(question: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    try:
        val = input(f"  {question} ({d}): ").strip().lower()
    except EOFError:
        return default_yes
    if not val:
        return default_yes
    return val in ("y", "yes")


def ask_download_now(size: int) -> bool:
    """Ask whether to download the (big) model file NOW or skip it for later.

    Returns True = download now, False = skip (every other init step runs as usual).
    """
    print()
    info(f"The chosen model file is ~{human(size)}.")
    while True:
        val = ask(
            "Download the model physically NOW, or SKIP it for later?\n"
            "      [D] Download now  (recommended - everything works out of the box)\n"
            "      [S] Skip for later  (init still does ALL other setup; re-run this init\n"
            "          any time to fetch the model - downloads resume where they stopped)",
            default="D").strip().lower()
        if val in ("d", "download"):
            return True
        if val in ("s", "skip"):
            return False
        warn("Please enter D (download now) or S (skip for later)")


def backup_once(path: str, stamp: str) -> None:
    """Copy *path* to <name>.bak-<stamp> once (idempotent within one run)."""
    bak = f"{path}.bak-{stamp}"
    if os.path.exists(path) and not os.path.exists(bak):
        shutil.copy2(path, bak)


def write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CLIENT UI TLS certificates (https://127.0.0.1:8000/)
# ---------------------------------------------------------------------------

def _gen_ui_certs(certs_dir: str) -> bool:
    """Generate ca.crt + server.crt/server.key for the CLIENT UI into *certs_dir*.

    Uses the 'cryptography' package - installed on demand (one small pip install,
    no venv needed). Returns True on success. The CA is imported into the browser
    once and then https://127.0.0.1:8000/ is trusted automatically afterwards.
    """
    try:
        import subprocess as _sp

        r = _sp.run([sys.executable, "-c", "import cryptography"], capture_output=True)
        if r.returncode != 0:
            info("Installing the 'cryptography' package (one small pip install)...")
            pr = _sp.run([sys.executable, "-m", "pip", "install", "--quiet", "cryptography"])
            if pr.returncode != 0:
                warn("'cryptography' could not be installed - skipping UI certificates.")
                return False
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_ui_certs.py")
        r2 = _sp.run([sys.executable, script, certs_dir], capture_output=True, text=True)
        if r2.returncode != 0:
            warn(f"UI certificate generation failed (exit {r2.returncode}) - skipping.")
            return False
        return True
    except Exception as e:
        warn(f"UI certificate generation problem ({e}) - skipping.")
        return False


def ensure_ui_certs(root: str) -> bool:
    """Make sure the CLIENT UI has TLS certs (fresh clones have none).

    Checks CLIENT/certs/ first, then <root>/certs/ - exactly the two locations
    entry/ssl_config.py looks at. If a pair is already there it is kept untouched;
    otherwise a local CA + leaf certificate are generated into CLIENT/certs/.
    Returns True when usable server.crt/server.key now exist (old or new).
    """
    client_certs = os.path.join(root, "CLIENT", "certs")
    root_certs = os.path.join(root, "certs")
    for d in (client_certs, root_certs):
        if os.path.isfile(os.path.join(d, "server.crt")) and os.path.isfile(os.path.join(d, "server.key")):
            ok(f"CLIENT UI certificates already present in {os.path.relpath(d, root)}\\ - kept untouched")
            return True

    info("No CLIENT UI certificates found (fresh clone) - generating a local CA + certificate...")
    made = _gen_ui_certs(client_certs)
    if not made:
        warn("CLIENT will start HTTP-only. You can re-run init later, or copy a server.crt/server.key "
             "pair into CLIENT\\certs\\ and restart the client.")
        return False

    ok(f"Generated CLIENT UI certificates in {os.path.relpath(client_certs, root)}\\ :")
    for f_ in ("ca.crt", "server.crt", "server.key"):
        fp = os.path.join(client_certs, f_)
        if os.path.isfile(fp):
            ok(f"  {fp}")

    print()
    info("One-time browser trust (do this ONCE per PC - the CA is valid for 10 years):")
    info(f"  1. Double-click {os.path.join(client_certs, 'ca.crt')} in File Explorer")
    info('     -> "Install Certificate" -> Local Machine -> "Trusted Root Certification Authorities".')
    info("  2. Restart the browser completely (close ALL windows).")
    info("     Afterwards https://127.0.0.1:8000/ is trusted automatically - no more warnings.")
    return True


# ---------------------------------------------------------------------------
# Download engine - resumable streaming with progress + size verification
# ---------------------------------------------------------------------------

def _http_get(url: str, resume_from: int = 0) -> tuple[int, int | None, object]:
    """Open *url* (following redirects). Returns (status, content_length, response)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": f"coolems-init/1.0 (python-urllib)",
        **({"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}),
    })
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        return e.code, None, e
    total = resp.headers.get("Content-Length")
    return resp.status, int(total) if total else None, resp


def download_file(url: str, dest_path: str, expected_size: int | None = None, label: str = "") -> None:
    """Download *url* to *dest_path*. Resumable via <dest>.part; verifies final size.

    Safe to re-run after a network drop - it continues where it left off.
    """
    if os.path.exists(dest_path):
        if expected_size is not None and os.path.getsize(dest_path) == expected_size:
            ok(f"{label or os.path.basename(dest_path)} already present ({human(expected_size)}) - skipped")
            return
        warn(f"Existing {os.path.basename(dest_path)} has wrong size "
             f"({human(os.path.getsize(dest_path))}) - re-downloading from scratch")
        os.remove(dest_path)

    part = dest_path + ".part"
    label = label or os.path.basename(dest_path)
    t0 = time.time()
    attempts = 0
    while True:
        attempts += 1
        have = os.path.getsize(part) if os.path.exists(part) else 0
        status, total, resp = _http_get(url, resume_from=have)

        if status == 206:                       # resumed OK
            mode = "ab"
            got = have
            size = (total or 0) + have          # Content-Length on 206 is the REMAINDER
        elif status == 416 and expected_size is not None and have >= expected_size:
            # Server says "nothing left to send" - our .part is already complete.
            os.replace(part, dest_path)
            ok(f"{label}: partial file was already complete ({human(have)}) - finalized")
            return
        elif status == 200:                     # fresh start (or server ignored Range)
            mode = "wb"                         # truncates any stale .part - correct restart
            got = 0
            size = total
        else:
            raise InitError(f"{label}: HTTP {status} from server - cannot download")

        is_tty = sys.stdout.isatty()
        last_draw = 0.0
        try:
            with open(part, mode) as f:
                while True:
                    chunk = resp.read(1 << 20)   # 1 MiB
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if is_tty and (now - last_draw > 0.3 or (size and got >= size)):
                        last_draw = now
                        if size:
                            pct = 100.0 * got / size
                            spd = got / max(now - t0, 0.001)
                            eta_s = (size - got) / max(spd, 1)
                            print(f"\r  [DL] {label[:34]:<34} {pct:5.1f}%  "
                                  f"{human(got)}/{human(size)}  {human(spd)}/s  ETA {int(eta_s//60):d}:{int(eta_s%60):02d}",
                                  end="", flush=True)
                        else:
                            print(f"\r  [DL] {label[:34]:<34} {human(got)}  "
                                  f"{human(got / max(now - t0, 0.001))}/s", end="", flush=True)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if is_tty:
                print()
            if attempts >= 5:
                raise InitError(
                    f"{label}: download failed after {attempts} tries ({e}). "
                    f"Partial file kept at {part} - fix the network and re-run init to resume."
                ) from e
            wait_s = min(2 ** attempts, 30)
            warn(f"{label}: connection problem ({e}) - retrying in {wait_s}s (attempt {attempts}/5)")
            time.sleep(wait_s)
            continue

        if is_tty:
            print()

        final_size = os.path.getsize(part)
        if size and final_size != size:
            raise InitError(f"{label}: size mismatch after download ({human(final_size)} of {human(size)})")
        if expected_size is not None and final_size != expected_size:
            raise InitError(
                f"{label}: downloaded file is {final_size} bytes but the known-good size is "
                f"{expected_size}. The file was NOT installed - re-run init to retry."
            )
        os.replace(part, dest_path)
        ok(f"{label}: {human(final_size)} in {time.time() - t0:.0f}s")
        return


def extract_flat(zip_path: str, target_dir: str) -> int:
    """Extract a zip FLAT into *target_dir* (strips one nesting level if present).

    Returns number of files written. Refuses to touch paths that would escape the dir.
    """
    os.makedirs(target_dir, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for item in zf.infolist():
            if item.is_dir():
                continue
            name = item.filename.replace("\\", "/")
            # Strip a single leading folder (e.g. "llama-b10441-bin-win-cuda-12.4-x64/llama-server.exe").
            parts = [p for p in name.split("/") if p not in ("", ".", "..")]
            if len(parts) > 1:
                parts = parts[1:]
            if not parts or any(p.startswith("..") for p in parts):
                continue
            dest = os.path.join(target_dir, *parts)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(item) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


# ---------------------------------------------------------------------------
# llama.cpp binaries
# ---------------------------------------------------------------------------

def install_llama_libs(gpu_name: str | None) -> None:
    root = ROOT
    ls_dir = os.path.join(root, "llama_server")
    exe = os.path.join(ls_dir, "llama-server.exe")
    if os.path.exists(exe):
        ok(f"llama-server.exe already present in llama_server/ - nothing to do "
           f"(you can manually upgrade it later: https://github.com/ggml-org/llama.cpp/releases)")
        return

    gpu_mode = gpu_name is not None
    choice = ask(
        "How should I get the llama.cpp binaries (pinned build " + LLAMA_BUILD + ")?\n"
        "      [A] Auto-download the pinned build this codebase targets (recommended)\n"
        "      [B] I will download a newer one by hand from GitHub releases",
        default="A").strip().lower()

    if choice == "b":
        print()
        info("Download a recent Windows x64 build here:")
        info(f"   https://github.com/ggml-org/llama.cpp/releases  (we pin {LLAMA_BUILD})")
        info("Then extract the zip contents FLAT into:  llama_server\\   "
             "(so llama-server.exe sits directly in that folder, no subfolder).")
        warn(f"IMPORTANT: newer builds must still support --spec-type draft-mtp and --fit. "
             f"If unsure, pick {LLAMA_BUILD}.")
        print()
        while not os.path.exists(exe):
            wait = ask("Waiting for you... when llama-server.exe is in place press Enter to check (or 'q' to quit)", default="")
            if wait.lower() == "q":
                raise InitError("Aborted - put llama-server.exe into llama_server/ and re-run init (it will then skip this step).")
            time.sleep(1)
        ok(f"Found {exe}")
        return

    # ---- auto path ----
    temp_dir = os.path.join(root, ".temp")
    os.makedirs(temp_dir, exist_ok=True)
    zip_name = "llama-b10441-bin-win-cuda-12.4-x64.zip" if gpu_mode else "llama-b10441-bin-win-cpu-x64.zip"
    zip_url = LLAMA_ZIP_CUDA if gpu_mode else LLAMA_ZIP_CPU
    zip_size = LLAMA_ZIP_CUDA_SIZE if gpu_mode else LLAMA_ZIP_CPU_SIZE
    zip_path = os.path.join(temp_dir, zip_name)

    info(f"Downloading llama.cpp {LLAMA_BUILD} ({'CUDA 12.4' if gpu_mode else 'CPU'} build, ~{human(zip_size)})...")
    download_file(zip_url, zip_path, expected_size=zip_size, label=zip_name)
    n = extract_flat(zip_path, ls_dir)
    ok(f"Extracted {n} files into llama_server/ (flat layout)")
    try:
        os.remove(zip_path)                      # reclaim the ~1.3 GB zip now that it is extracted
    except OSError:
        pass

    # CUDA runtime DLLs - only needed when the machine has no CUDA 12 toolkit yet.
    if gpu_mode:
        need_cudart = not any(
            d.startswith(("cublas64_12", "cudart64_12")) for d in os.listdir(ls_dir) if d.endswith(".dll")
        )
        if need_cudart:
            info("No CUDA 12 runtime DLLs found - downloading cudart-llama (CUDA 12.4, ~373 MB)...")
            cudart_zip = os.path.join(temp_dir, "cudart-llama-bin-win-cuda-12.4-x64.zip")
            download_file(CUDART_ZIP_124, cudart_zip, expected_size=CUDART_ZIP_SIZE, label="cudart 12.4")
            n2 = extract_flat(cudart_zip, ls_dir)
            ok(f"Extracted {n2} CUDA runtime files into llama_server/")
            try:
                os.remove(cudart_zip)
            except OSError:
                pass

    if not os.path.exists(exe):
        raise InitError("llama-server.exe is still missing after extraction - check the zip contents and re-run init.")
    ok("llama.cpp binaries installed")


# ---------------------------------------------------------------------------
# Model selection + downloads
# ---------------------------------------------------------------------------

def mtp_rename(filename: str) -> str:
    """Insert '-MTP' before the quant suffix so _is_mtp_model() picks the file up.

    Qwen3.8-27B-UD-IQ1_M.gguf  ->  Qwen3.8-27B-UD-MTP-IQ1_M.gguf
    (only when 'mtp' is not already in the name)
    """
    if "mtp" in filename.lower():
        return filename
    stem, ext = os.path.splitext(filename)
    for quant, _fname, _size in sorted(QUANTS, key=lambda q: -len(q[0])):
        suffix = f"-{quant}"
        if stem.lower().endswith(suffix.lower()):
            return stem[: -len(suffix)] + "-MTP" + suffix + ext
    # Unknown shape - insert before the last dash as a safe fallback.
    idx = stem.rfind("-")
    return (stem[:idx] + "-MTP" + stem[idx:]) if idx > 0 else "MTP-" + filename


def select_model(gpu_name: str | None, vram_mb: int | None) -> tuple[dict, dict]:
    """Interactive model menu. Returns (quant_entry, tier)."""
    tier = tier_for_vram(vram_mb)

    header("STEP 2 - MODEL SELECTION")
    if gpu_name:
        info(f"Detected GPU: {gpu_name} ({human(vram_mb)} VRAM)")
    else:
        warn("No NVIDIA GPU detected (nvidia-smi unavailable or no NVIDIA card).")
        warn("You can still install for CPU-only use, but expect very slow inference.")

    print()
    info(f"Recommended for your GPU - tier {tier['id']}:  Qwen3.8-27B-{tier['quant']}  ({tier['note']})")
    print()
    print("  Available quants (unsloth/Qwen3.8-27B-GGUF on HuggingFace):")
    print(f"   {'#':>2}  {'QUANT':<9} {'SIZE':>9}")
    for i, (q, _f, s) in enumerate(QUANTS, start=1):
        mark = "  <-- recommended" if q == tier["quant"] else ""
        print(f"   {i:>2}  {q:<9} {human(s):>9}{mark}")
    print()

    rec_idx = next(i for i, (q, _f, _s) in enumerate(QUANTS, start=1) if q == tier["quant"])
    while True:
        val = ask(f"Pick a quant number [Enter = {rec_idx} ({tier['quant']})]", default=str(rec_idx))
        try:
            idx = int(val)
            if 1 <= idx <= len(QUANTS):
                return QUANTS[idx - 1], tier
        except ValueError:
            pass
        warn("Please enter a number between 1 and " + str(len(QUANTS)))


def download_model_files(ctx: dict) -> None:
    """Downloads main model (+ optional OCR). Fills ctx keys.

    With ctx['download_now'] False the big model file is NOT fetched - only its target
    name/path are computed so the config wiring works exactly as usual; re-running init
    any time later fetches it (resumable). The OCR download is unaffected by that choice.
    """
    quant, fname, size = ctx["quant_entry"]
    model_folder = ctx["model_folder"]
    os.makedirs(model_folder, exist_ok=True)

    target_name = mtp_rename(fname) if ctx["want_mtp"] else fname

    if not ctx.get("download_now", True):
        # ---- SKIP mode: no physical download; pre-seed the expected name/path so the
        # config wiring below works exactly as usual. Re-running init fetches it then.
        existing_p = os.path.join(model_folder, target_name)
        if os.path.exists(existing_p) and os.path.getsize(existing_p) == size:
            ok(f"{target_name} already present ({human(size)}) - nothing to fetch later")
        else:
            print()
            info("Model file NOT downloaded (skipped for later). It will live at:")
            info(f"   {existing_p}")
            info("Re-run ZZZ_initial_init.bat any time to fetch it - the download is resumable.")
        ctx["final_model_name"] = target_name
    else:
        # Reuse an existing correct file (either naming variant), enforcing the MTP rename rule.
        found = None
        for candidate in (target_name, fname):
            p = os.path.join(model_folder, candidate)
            if os.path.exists(p) and os.path.getsize(p) == size:
                found = p
                break

        if found is not None:
            target = mtp_rename(fname) if ctx["want_mtp"] else fname
            if os.path.basename(found) != target:
                os.replace(found, os.path.join(model_folder, target))
                ok(f"Renamed existing file -> {target}")
            else:
                ok(f"{target} already present ({human(size)}) - skipped")
            ctx["final_model_name"] = target
        else:
            plain_p = os.path.join(model_folder, fname)
            url = f"https://huggingface.co/{HF_MODEL_REPO}/resolve/main/{fname}?download=true"
            download_file(url, plain_p, expected_size=size, label=f"Qwen3.8-27B-{quant}")
            if target_name != fname:
                os.replace(plain_p, os.path.join(model_folder, target_name))
                ok(f"Renamed -> {target_name}  (the 'MTP' in the name is what enables "
                   f"speculative decoding in our code)")
            ctx["final_model_name"] = target_name

    ctx["model_path"] = os.path.join(model_folder, ctx["final_model_name"])

    # ---- OCR model ----
    if ctx["want_ocr"]:
        ocr_folder = os.path.join(ctx["models_root"], "glm_ocr")
        os.makedirs(ocr_folder, exist_ok=True)
        for ofname, osize in OCR_FILES:
            url = f"https://huggingface.co/{HF_OCR_REPO}/resolve/main/{ofname}?download=true"
            download_file(url, os.path.join(ocr_folder, ofname), expected_size=osize, label=ofname)
        ctx["ocr_folder"] = ocr_folder


# ---------------------------------------------------------------------------
# Config wiring (server <-> client sync)
# ---------------------------------------------------------------------------

def _is_placeholder_key(entry: dict) -> bool:
    key = str(entry.get("key", ""))
    return (not key) or ("PASTE" in key.upper()) or ("EXAMPLE" in key.upper())


def wire_configs(ctx: dict) -> None:
    root = ctx["root"]
    stamp = time.strftime("%Y%m%d_%H%M%S")

    print(); print(line("-")); print("   config wiring (server <-> client sync)"); print(line("-"))

    # ---- 6a. run both official bootstraps so every required file exists ----
    # Each bootstrap runs as its own interpreter with the right PYTHONPATH (the SERVER
    # 'config' package and the CLIENT's 'CLIENT/config' package must not mix in one process).
    for parts, env_root in (
        (("config", "bootstrap.py"), root),
        (("CLIENT", "config", "bootstrap.py"), os.path.join(root, "CLIENT")),
    ):
        rel = "/".join(parts)
        script = os.path.join(root, *parts)
        if not os.path.exists(script):
            warn(f"{rel} missing - skipping its bootstrap")
            continue
        env = dict(os.environ, PYTHONPATH=env_root)
        try:
            r = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
        except OSError as e:                       # exotic systems / AV blocking python - keep going
            warn(f"could not run {rel} ({e}) - continuing; missing files will self-heal at boot")
            continue
        (ok if r.returncode == 0 else warn)(f"ran {rel}" + ("" if r.returncode == 0 else f" (exit {r.returncode})"))

    # ---- 6b. API key into the SERVER file; bookkeeping only into the CLIENT file ----
    # SECURITY (2026-09-29): the CLIENT's .api_client_keys.json must NEVER hold the real
    # key (or role/is_active/max_connections/last_used) - it stores {email, date_acquired}
    # only. The authoritative credential stays in config/.api_keys.json on this machine;
    # headless CLIENT startup reads the key from the COOLEMS_CLIENT_API_KEY env var (set
    # below), and interactive use gets it from the browser UI (localStorage).
    api_key = ctx["api_key"]
    email = ctx["email"]

    # 6b-i. SERVER file: full credential entry (this is where the real key belongs).
    server_entry = {
        "key": api_key,
        "email": email,
        "role": "admin",
        "date_acquired": None,
        "is_active": True,
        "max_connections": 10,
        "last_used": None,
    }
    server_path = os.path.join(root, "config", ".api_keys.json")
    if not os.path.exists(server_path):       # bootstrap should have created it; be safe
        write_json(server_path, [server_entry])
        ok("config/.api_keys.json: created with new API key")
    else:
        try:
            with open(server_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                entries = []
        except (json.JSONDecodeError, OSError):
            warn("config/.api_keys.json unreadable - backing up and rewriting")
            backup_once(server_path, stamp)
            entries = []
        real = [e for e in entries if isinstance(e, dict) and not _is_placeholder_key(e)]
        changed = False
        kept = [e for e in entries if isinstance(e, dict) and (not _is_placeholder_key(e))]
        if len(kept) != len(entries):
            changed = True
        if not any(isinstance(e, dict) and e.get("key") == api_key for e in kept):
            kept.append(server_entry)
            changed = True
        if changed:
            backup_once(server_path, stamp)
            write_json(server_path, kept)
            ok("config/.api_keys.json: API key installed" + (f" (+{len(real)} existing real key(s) kept)" if real else ""))
        else:
            ok("config/.api_keys.json: already in sync - untouched")

    # 6b-ii. CLIENT file: NON-CONFIDENTIAL bookkeeping only ({email, date_acquired}).
    client_path = os.path.join(root, "CLIENT", "config", ".api_client_keys.json")
    if not os.path.exists(client_path):       # bootstrap should have created it; be safe
        write_json(client_path, [{"email": email, "date_acquired": None}])
        ok("CLIENT/config/.api_client_keys.json: created with bookkeeping entry (no key on client disk)")
    else:
        try:
            with open(client_path, "r", encoding="utf-8") as f:
                c_entries = json.load(f)
            if not isinstance(c_entries, list):
                c_entries = []
        except (json.JSONDecodeError, OSError):
            warn("CLIENT/config/.api_client_keys.json unreadable - backing up and rewriting")
            backup_once(client_path, stamp)
            c_entries = []
        # Strip any legacy confidential fields that older builds may have written here.
        c_cleaned = []
        for e in c_entries:
            if not isinstance(e, dict):
                continue
            c_email = e.get("email") or email
            c_cleaned.append({"email": c_email, "date_acquired": e.get("date_acquired")})
        had_legacy = any(isinstance(e, dict) and ("key" in e or "role" in e) for e in c_entries)
        # Ensure a bookkeeping row exists (keeps the user's original date_acquired).
        if not any((e.get("email") or "") == email for e in c_cleaned):
            c_cleaned.append({"email": email, "date_acquired": None})
        write_json(client_path, c_cleaned)
        ok("CLIENT/config/.api_client_keys.json: bookkeeping synced (email + date only - key never stored client-side)"
           + (" [legacy confidential fields stripped]" if had_legacy else ""))

    # 6b-iii. Headless CLIENT startup needs the key without any UI -> environment variable.
    os.environ["COOLEMS_CLIENT_API_KEY"] = api_key
    try:
        import subprocess as _sp
        _sp.run(["setx", "COOLEMS_CLIENT_API_KEY", api_key], capture_output=True, text=True, timeout=15)
        ok("COOLEMS_CLIENT_API_KEY persisted via setx (new shells/launchers pick it up automatically)")
    except Exception:
        warn("could not persist COOLEMS_CLIENT_API_KEY via setx - set it manually for headless CLIENT startup")
    
    # ---- 6c. profiles.json -> this machine's real folders ----
    prof_path = os.path.join(root, "config", "profiles.json")
    if os.path.exists(prof_path):
        try:
            with open(prof_path, "r", encoding="utf-8") as f:
                profiles = json.load(f)
            if not isinstance(profiles, dict):
                raise ValueError("not a dict")
        except (json.JSONDecodeError, OSError, ValueError) as e:
            warn(f"profiles.json unreadable ({e}) - leaving it for manual fix; model folder is "
                 f"{ctx['model_folder']}")
            profiles = None
    else:
        profiles = None
        warn(f"profiles.json not found (bootstrap should create it) - model folder is {ctx['model_folder']}")

    if isinstance(profiles, dict):
        changed_roles = []
        for role, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            before = (profile.get("allowed_models_folders"), profile.get("ocr_model"))
            profile["allowed_models_folders"] = [ctx["model_folder"]]
            if ctx.get("ocr_folder"):
                profile["ocr_model"] = ctx["ocr_folder"]
            else:
                # OCR declined - clear stale paths from other machines (consumers skip empty).
                profile["ocr_model"] = ""
            after = (profile["allowed_models_folders"], profile.get("ocr_model"))
            if before != after:
                changed_roles.append(role)
        if changed_roles:
            backup_once(prof_path, stamp)
            write_json(prof_path, profiles)
            ok(f"profiles.json: model/OCR paths rewired for {len(changed_roles)} profile(s): "
               + ", ".join(sorted(changed_roles)))
        else:
            ok("profiles.json: already pointing at the right folders - untouched")

    # ---- 6d. pre-seed last-loaded model so SERVER boots straight into it ----
    last_path = os.path.join(root, "config", ".last_model_loaded.json")
    payload = {"model": ctx["final_model_name"], "path": ctx["model_path"]}
    current = None
    if os.path.exists(last_path):
        try:
            with open(last_path, "r", encoding="utf-8") as f:
                current = json.load(f)
        except (json.JSONDecodeError, OSError):
            backup_once(last_path, stamp)
    if current != payload:
        if os.path.exists(last_path):
            backup_once(last_path, stamp)
        write_json(last_path, payload)
        ok(f".last_model_loaded.json: SERVER will boot into {ctx['final_model_name']}")

    # ---- 6e. patch CONTEXT_WINDOW_TOKENS to the tier value ----
    cfg_py = os.path.join(root, "config", "config.py")
    want_ctx = ctx["tier"]["ctx"]
    try:
        with open(cfg_py, "r", encoding="utf-8") as f:
            src = f.read()
        new_src, n = re.subn(r"(CONTEXT_WINDOW_TOKENS:\s*int\s*=\s*)\d+", rf"\g<1>{want_ctx}", src, count=1)
        if n == 0:
            warn("Could not find CONTEXT_WINDOW_TOKENS in config/config.py - left as-is")
        elif new_src != src:
            backup_once(cfg_py, stamp)
            with open(cfg_py, "w", encoding="utf-8") as f:
                f.write(new_src)
            ok(f"config/config.py: CONTEXT_WINDOW_TOKENS -> {want_ctx} (tier {ctx['tier']['id']})")
        else:
            ok(f"config/config.py: CONTEXT_WINDOW_TOKENS already {want_ctx} - untouched")
    except OSError as e:
        warn(f"Could not patch config/config.py: {e}")

    # ---- 6f. CLIENT settings + working root ----
    client_cfg_dir = os.path.join(root, "CLIENT", "config")
    settings_path = os.path.join(client_cfg_dir, "settings.json")
    if ctx["same_machine_client"]:
        data = {}
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            pass
        local_addr = "localhost:8080"          # repo convention: index 0 of the failover list
        addrs = [a for a in (data.get("server_addresses") or []) if isinstance(a, str) and a.strip()]
        changed_client = False
        if local_addr not in addrs:
            addrs.insert(0, local_addr)
            changed_client = True
        data["server_addresses"] = addrs
        if data.get("server_address") != local_addr:
            data["server_address"] = local_addr
            changed_client = True
        if changed_client:
            backup_once(settings_path, stamp)
            write_json(settings_path, data)
            ok(f"CLIENT settings.json: {local_addr} set as primary server address (same machine)")
        else:
            ok("CLIENT settings.json: already pointing at localhost - untouched")
    else:
        info("CLIENT will run on another PC - leaving its settings.json for auto-detect "
             "(it finds the SERVER's LAN IP on first boot; make sure both PCs are on the same network).")

    wr_path = os.path.join(client_cfg_dir, ".working_root.json")
    data = {}
    try:
        with open(wr_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (json.JSONDecodeError, OSError):
        pass
    if data.get("working_root") != root:
        data["working_root"] = root
        backup_once(wr_path, stamp)
        write_json(wr_path, data)
        ok(f"CLIENT .working_root.json -> {root}")

    # ---- 6g. CLIENT UI TLS certificates (browser trust for https://127.0.0.1:8000/) ----
    ctx["ui_certs_ok"] = ensure_ui_certs(root)


def _print_browser_steps(num: int, ui_url: str, where: str = "") -> None:
    """Standard browser login steps (open UI -> Settings email/key -> CONNECT -> refresh)."""
    info(f"  {num}. Open {ui_url} in a browser{where}.")
    info("     Expect NO model loaded yet - that is normal until you log in:")
    info("         open Settings -> enter the EMAIL + API KEY printed above -> press CONNECT.")
    info(f"  {num + 1}. Refresh the page (F5) - the model list now appears and you can start chatting.")


def _print_portable_client_block() -> None:
    """The [a]/[b] options for running this SERVER's client on ANOTHER PC.

    The CLIENT folder is self-contained: its API key file, settings.json, UI certs and the
    ZZZ_CLIENT.bat venv bootstrap all live INSIDE it (see working_root.py - the tree is
    designed to be moved; a stale .working_root.json self-heals on that PC).
    """
    info("     [a] FASTEST (no init there): copy the whole CLIENT\\ folder from THIS repo to that")
    info("         PC - it already contains its API key file + settings. That PC only needs Python >= 3.10,")
    info(f"         then just run ZZZ_CLIENT.bat there (first run builds its venv).")
    info("     [b] Or clone the repo on that PC and run ZZZ_initial_init.bat there too.")
    info("     File tools on that PC work against its own 'Working Folder' (settable in the UI).")


def _print_ca_note() -> None:
    """One-time browser trust for a SECOND PC when init generated the UI certificates."""
    info("     If UI certificates were generated, import CLIENT\\certs\\ca.crt ONCE on that PC")
    info('         (Trusted Root CAs) so its browser trusts https://127.0.0.1:8000/ automatically.')


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # <repo>/utils/zzz_init.py -> <repo>


def main() -> int:
    _utf8_console()
    if sys.version_info < MIN_PYTHON:
        print(f"\n  [ERROR] Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required (you have "
              f"{sys.version_info.major}.{sys.version_info.minor}).")
        print("  Install it from https://www.python.org/downloads/ and re-run.\n")
        return 2

    print()
    print(line("="))
    print("   COOLEMS - ZZZ initial init (fresh-clone setup)")
    print(f"   Repo root: {ROOT}")
    print(line("="))

    if not os.path.isdir(os.path.join(ROOT, "llama_server")) or \
       not os.path.isfile(os.path.join(ROOT, "config", "config.py")):
        raise InitError("This does not look like the COOLEMS repo root. Run ZZZ_initial_init.bat from the cloned repo.")

    try:
        # ---- STEP 1: GPU detection -------------------------------------------
        header("STEP 1 - HARDWARE DETECTION")
        gpu_name, vram_mb = detect_gpu()
        if gpu_name:
            ok(f"GPU: {gpu_name} ({human(vram_mb)} VRAM)")
        else:
            warn("No NVIDIA GPU found via nvidia-smi. Continuing in CPU-only mode (slow).")

        # ---- STEP 2: model menu ----------------------------------------------
        quant_entry, tier = select_model(gpu_name, vram_mb)
        q_label, _q_fname, q_size = quant_entry
        ok(f"Model choice: Qwen3.8-27B-{q_label} ({human(q_size)}) - context target {tier['ctx']} tokens")

        # ---- STEP 2a: download the model now, or skip it for later ------------
        header("STEP 2a - DOWNLOAD NOW OR SKIP FOR LATER")
        download_now = ask_download_now(q_size)
        if download_now:
            ok(f"Will download the model file now (~{human(q_size)})")
        else:
            warn("Model download SKIPPED for later - everything else is set up as usual. "
                 "Re-run ZZZ_initial_init.bat any time to fetch it (resumable).")

        # ---- STEP 2b: optional downloads + disk pre-check ---------------------
        header("STEP 2b - OPTIONAL DOWNLOADS & DISK CHECK")
        want_mtp = ask_yn(
            "Enable MTP speculative decoding (>2x generation speedup)?\n"
            "      (no extra download - the head is already inside unsloth's GGUF; the file just gets\n"
            "       renamed with 'MTP' so our code auto-enables it. On very small GPUs say N)",
            default_yes=True)
        want_ocr = ask_yn(
            f"Download the GLM-OCR model (+{human(sum(s for _f, s in OCR_FILES))}, needed by the transcribe_image tool)?",
            default_yes=True)

        need = (q_size if download_now else 0) + sum(s for _f, s in OCR_FILES if want_ocr)
        need = int(need * 1.25) + (1_500_000_000 if not os.path.exists(os.path.join(ROOT, "llama_server", "llama-server.exe")) else 0)
        free = shutil.disk_usage(ROOT).free
        if free < need:
            raise InitError(
                f"Not enough disk space: need ~{human(need)} free on the drive holding this repo, "
                f"only {human(free)} available. Free up space and re-run init (downloads resume where they stopped)."
            )
        ok(f"Disk space OK ({human(free)} free, ~{human(need)} needed)")

        # ---- llama.cpp binaries ----------------------------------------
        header("STEP 3 - LLAMA.CPP BINARIES")
        install_llama_libs(gpu_name)

        # ---- model + optional file downloads (or skipped for later) ---------------------------
        if download_now:
            header("STEP 4 - MODEL DOWNLOADS")
        else:
            header("STEP 4 - MODEL DOWNLOADS (SKIPPED FOR LATER)")
        ctx = {
            "root": ROOT,
            "quant_entry": quant_entry,
            "tier": tier,
            "want_mtp": want_mtp,
            "want_ocr": want_ocr,
            "download_now": download_now,
            "models_root": os.path.join(ROOT, "llama_server", "models"),
            "model_folder": os.path.join(ROOT, "llama_server", "models", f"qwen38-27b-{q_label.lower()}"),
        }
        download_model_files(ctx)

        # ---- credentials + config wiring --------------------------------------------
        header("STEP 5 - CREDENTIALS & CONFIG WIRING")
        email = ask("Email address for the admin API key (used to log into the UI)", default="admin@localhost")
        api_key = secrets.token_hex(32)
        ctx.update({"email": email, "api_key": api_key})
        ctx["same_machine_client"] = ask_yn("Will the CLIENT run on THIS same PC?", default_yes=True)
        wire_configs(ctx)

        # ---- FINAL REPORT ---------------------------------------------------------
        header("INIT COMPLETE")
        print()
        ok(f"llama.cpp {LLAMA_BUILD}:      llama_server\\llama-server.exe")
        if ctx.get("download_now", True):
            ok(f"Model:                        {ctx['model_path']}")
        else:
            warn(f"Model (SKIPPED for later):  {ctx['model_path']}")
        if want_mtp:
            ok("MTP spec decoding ENABLED - head is built into the model file (no extra weights loaded)")
        if want_ocr:
            ok(f"OCR model:                  {ctx['ocr_folder']}")
        print()
        info("API key for UI login (saved in config/.api_keys.json; CLIENT keeps only email + date bookkeeping):")
        print()
        print(f"      EMAIL:  {email}")
        print(f"      KEY:    {api_key}")
        print()
        info(f"Context window set to {tier['ctx']} tokens (config/config.py) - matches your GPU tier.")
        if ctx.get("ui_certs_ok"):
            print()
            info("UI certificates: CLIENT will serve https://127.0.0.1:8000/ with a locally trusted CA.")
            info("   One-time import of the CA (CLIENT\\certs\\ca.crt) was printed above - do it once per PC.")
        print()
        ui_url = "https://127.0.0.1:8000/" if ctx.get("ui_certs_ok") else "http://127.0.0.1:8000/"
        info("Next steps:")
        step = 1
        if not ctx.get("download_now", True):
            info(f"  {step}. Re-run ZZZ_initial_init.bat to download the model file you skipped")
            info("         (it picks up where it left off - everything else is already set up).")
            step += 1
        info(f"  {step}. Run ZZZ_SERVER.bat   (first run creates the venv + installs deps - takes a few minutes)")
        if ctx["same_machine_client"]:
            info(f"  {step + 1}. Run CLIENT\\ZZZ_CLIENT.bat")
            _print_browser_steps(step + 2, ui_url)
            print()
            info("Want to use this SERVER from ANOTHER PC too? The CLIENT folder is portable:")
            _print_portable_client_block()
            info("   Both PCs must be on the same network - on first boot the client finds this")
            info("   SERVER's LAN IP automatically (no address editing needed). Then log in on that PC")
            info(f"   exactly like above: {ui_url} -> Settings -> SAME EMAIL + API KEY -> CONNECT -> refresh.")
            if ctx.get("ui_certs_ok"):
                _print_ca_note()
        else:
            info(f"  {step + 1}. On the OTHER PC, get the client running there (the CLIENT folder is portable):")
            _print_portable_client_block()
            info("     Both PCs must be on the same network - on first boot the client finds this")
            info("     SERVER's LAN IP automatically (no address editing needed).")
            _print_browser_steps(step + 2, ui_url, where=" ON THAT PC")
            if ctx.get("ui_certs_ok"):
                _print_ca_note()

        print()
        return 0

    except InitError as e:
        print()
        warn(str(e))
        return 1
    except KeyboardInterrupt:
        print("\n\n  Aborted by user. Partial downloads are kept - re-run init to resume.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
