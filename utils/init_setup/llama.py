"""llama.cpp binaries - pinned build download or manual install wait."""

import os
import time

from .constants import (CUDART_ZIP_124, CUDART_ZIP_SIZE, LLAMA_BUILD, LLAMA_ZIP_CPU,
                        LLAMA_ZIP_CPU_SIZE, LLAMA_ZIP_CUDA, LLAMA_ZIP_CUDA_SIZE)
from .downloads import DownloadBlockedBySsl, download_file, extract_flat
from .errors import InitError
from .paths import ROOT
from .ui import ask, ask_yn, human, info, ok, warn


def _wait_for_manual_llama(exe: str) -> None:
    """Block until the user has placed llama-server.exe in *exe*'s folder (or quits)."""
    print()
    while not os.path.exists(exe):
        wait = ask("Waiting for you... when llama-server.exe is in place press Enter to check (or 'q' to quit)", default="")
        if wait.lower() == "q":
            raise InitError("Aborted - put llama-server.exe into llama_server/ and re-run init (it will then skip this step).")
        time.sleep(1)
    ok(f"Found {exe}")


def _manual_instructions(gpu_mode: bool) -> None:
    """Print where to get the binaries by hand and exactly where to unzip them."""
    print()
    info("Download a recent Windows x64 build here (we pin " + LLAMA_BUILD + "):")
    info(f"   https://github.com/ggml-org/llama.cpp/releases  -> release {LLAMA_BUILD}")
    if gpu_mode:
        info(f"   Pick the CUDA asset for your toolkit version, e.g.: {LLAMA_ZIP_CUDA}")
        # The pinned zip already bundles the CUDA runtime DLLs; older/newer releases may not.
        need_cudart_hint = ("If that build does NOT include cublas/cudart 12.* DLLs, also download "
                            f"{CUDART_ZIP_124} and extract it into the same folder.")
    else:
        info(f"   Pick the CPU asset, e.g.: {LLAMA_ZIP_CPU}")
        need_cudart_hint = ""
    info("Then extract the zip contents FLAT into:  llama_server\\")
    info("   (so llama-server.exe sits DIRECTLY in that folder - no subfolder).")
    if need_cudart_hint:
        warn(need_cudart_hint)
    warn(f"IMPORTANT: newer builds must still support --spec-type draft-mtp and --fit. "
         f"If unsure, pick {LLAMA_BUILD}.")


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
        _manual_instructions(gpu_mode)
        _wait_for_manual_llama(exe)
        return

    # ---- auto path ----
    temp_dir = os.path.join(root, ".temp")
    os.makedirs(temp_dir, exist_ok=True)
    zip_name = "llama-b10441-bin-win-cuda-12.4-x64.zip" if gpu_mode else "llama-b10441-bin-win-cpu-x64.zip"
    zip_url = LLAMA_ZIP_CUDA if gpu_mode else LLAMA_ZIP_CPU
    zip_size = LLAMA_ZIP_CUDA_SIZE if gpu_mode else LLAMA_ZIP_CPU_SIZE
    zip_path = os.path.join(temp_dir, zip_name)

    info(f"Downloading llama.cpp {LLAMA_BUILD} ({'CUDA 12.4' if gpu_mode else 'CPU'} build, ~{human(zip_size)})...")
    try:
        download_file(zip_url, zip_path, expected_size=zip_size, label=zip_name)
    except DownloadBlockedBySsl as e:
        # Python's TLS verification fails on this machine (missing CA bundle / MITM proxy),
        # but the user's BROWSER usually still works - point him to a manual download.
        print()
        warn(str(e))
        if ask_yn("Your web browser can probably open GitHub fine even though Python cannot -\n"
                  "download the binaries by hand instead?", default_yes=True):
            _manual_instructions(gpu_mode)
            _wait_for_manual_llama(exe)
            return
        raise InitError(
            "Aborted. Fix Python's certificates (see above), or re-run init and choose manual download."
        ) from e

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
