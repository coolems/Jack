"""Model selection + downloads (main model, vision projector, OCR)."""

import os

from .constants import HF_MMPROJ_FILE, HF_OCR_REPO, MMPROJ_SIZE, OCR_FILES, QUANTS
from .downloads import download_file
from .gpu import tier_for_vram
from .ui import ask, header, human, info, ok, warn


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
    print("  Available quants (HuggingFace):")
    print(f"   {'#':>2}  {'QUANT':<9} {'SIZE':>9}  REPO")
    for i, (q, _f, s, repo) in enumerate(QUANTS, start=1):
        mark = "  <-- recommended" if q == tier["quant"] else ""
        print(f"   {i:>2}  {q:<9} {human(s):>9}  {repo}{mark}")
    print()

    rec_idx = next(i for i, (q, _f, _s, _r) in enumerate(QUANTS, start=1) if q == tier["quant"])
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
    """Downloads main model (+ optional vision projector + optional OCR). Fills ctx keys.

    With ctx['download_now'] False the big model file is NOT fetched - only its target
    name/path are computed so the config wiring works exactly as usual; re-running init
    any time later fetches it (resumable). The mmproj download honors ctx['mmproj_now']
    the same way, and the OCR download is unaffected by either choice.

    Note: no MTP renaming happens here - both unsloth's and Dirk's GGUFs ship the MTP
    head baked into the file, and server-side detection reads it from the GGUF header
    (app/providers/llama/model_utils.py::_is_mtp_model), so the original filename works.
    """
    quant, fname, size, repo = ctx["quant_entry"]
    model_folder = ctx["model_folder"]
    os.makedirs(model_folder, exist_ok=True)

    if not ctx.get("download_now", True):
        # ---- SKIP mode: no physical download; pre-seed the expected name/path so the
        # config wiring below works exactly as usual. Re-running init fetches it then.
        existing_p = os.path.join(model_folder, fname)
        if os.path.exists(existing_p) and os.path.getsize(existing_p) == size:
            ok(f"{fname} already present ({human(size)}) - nothing to fetch later")
        else:
            print()
            info("Model file NOT downloaded (skipped for later). It will live at:")
            info(f"   {existing_p}")
            info(f"(source: huggingface.co/{repo})")
            info("Re-run ZZZ_initial_init.bat any time to fetch it - the download is resumable.")
        ctx["final_model_name"] = fname
    else:
        found_p = os.path.join(model_folder, fname)
        if os.path.exists(found_p) and os.path.getsize(found_p) == size:
            ok(f"{fname} already present ({human(size)}) - skipped")
        else:
            url = f"https://huggingface.co/{repo}/resolve/main/{fname}?download=true"
            download_file(url, found_p, expected_size=size, label=f"Qwen3.8-27B-{quant}")
        ctx["final_model_name"] = fname

    ctx["model_path"] = os.path.join(model_folder, ctx["final_model_name"])

    # ---- vision projector (mmproj) - lives NEXT TO the main model so the server's
    # _find_generic_mmproj() picks it up from the model folder automatically.
    if ctx.get("mmproj_now", True):
        url = f"https://huggingface.co/{repo}/resolve/main/{HF_MMPROJ_FILE}?download=true"
        download_file(url, os.path.join(model_folder, HF_MMPROJ_FILE),
                      expected_size=MMPROJ_SIZE, label=HF_MMPROJ_FILE)
    else:
        print()
        info("Vision projector NOT downloaded (skipped for later). It will live at:")
        info(f"   {os.path.join(model_folder, HF_MMPROJ_FILE)}")
        warn("Image input will not work until it is fetched - re-run ZZZ_initial_init.bat "
             "any time to download it (resumable).")

    # ---- OCR model ----
    if ctx["want_ocr"]:
        ocr_folder = os.path.join(ctx["models_root"], "glm_ocr")
        os.makedirs(ocr_folder, exist_ok=True)
        for ofname, osize in OCR_FILES:
            url = f"https://huggingface.co/{HF_OCR_REPO}/resolve/main/{ofname}?download=true"
            download_file(url, os.path.join(ocr_folder, ofname), expected_size=osize, label=ofname)
        ctx["ocr_folder"] = ocr_folder
