"""llama.cpp binaries - pinned build download or manual install wait."""

import os
import time

from .constants import (CUDART_ZIP_124, CUDART_ZIP_SIZE, LLAMA_BUILD, LLAMA_ZIP_CPU,
                        LLAMA_ZIP_CPU_SIZE, LLAMA_ZIP_CUDA, LLAMA_ZIP_CUDA_SIZE)
from .downloads import download_file, extract_flat
from .errors import InitError
from .paths import ROOT
from .ui import ask, human, info, ok, warn


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
