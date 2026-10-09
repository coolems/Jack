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
  3. Asks whether to download the VISION PROJECTOR (mmproj) from HuggingFace NOW or skip
     it for later - same D/S approach as the main model. Without it the model cannot see
     images; with it, image input works out of the box.
  4. Optional MTP: renames the main model so "MTP" is in its filename - that is what our
      code (_is_mtp_model) looks for to auto-enable speculative decoding (>2x speedup).
      The MTP head itself is ALREADY baked into unsloth's GGUF, so nothing extra is
      downloaded or loaded (llama.cpp builds the draft context from the same file).
  5. Optional GLM-OCR model from HuggingFace (needed by the transcribe_image tool).
  6. llama.cpp binaries: auto-download of the PINNED build this codebase is written
     against (b10441) - or you can download a newer one by hand and we wait for it.
  7. Config wiring (the server<->client sync part): generates an API key that lands in
     BOTH config/.api_keys.json AND CLIENT/config/.api_client_keys.json, rewrites all
     profile model/OCR paths to THIS machine's real folders, pre-seeds the last-loaded
     model so SERVER boots straight into it, and patches CONTEXT_WINDOW_TOKENS to a
     value your GPU can actually hold.

No venv work (ZZZ_SERVER.bat / ZZZ_CLIENT.bat self-bootstrap on first run) and no smoke
test - init ends with a plain report.

Usage:  python utils\\zzz_init.py        (or just double-click ZZZ_initial_init.bat)

The implementation lives in the utils/init_setup/ package (one module per concern):
    constants.py   verified URLs / sizes / quant table (HuggingFace + GitHub)
    errors.py      InitError
    paths.py       repo ROOT + CLIENT_UI_PORT reader (no config-package import allowed)
    ui.py          console output + interactive ask helpers + tiny file helpers
    gpu.py         nvidia-smi detection + VRAM tier table
    downloads.py   resumable HTTP download engine + flat zip extraction
    certs.py       CLIENT UI TLS certificate generation
    llama.py       llama.cpp binary install (pinned build)
    models.py      model selection menu + model/mmproj/OCR downloads
    wiring.py      config wiring (API keys, profiles, context window, client settings)
    main.py        the step-by-step init flow
"""

import os
import sys

# Ensure the repo ROOT is on sys.path so `utils.init_setup` resolves no matter where this
# script is launched from (e.g. ZZZ_initial_init.bat runs it as a plain file, which puts
# utils/ - not the repo root - first on sys.path).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # <repo>/utils/zzz_init.py -> <repo>
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.init_setup.main import main  # noqa: F401  (re-exported for `python -m`-style use)

if __name__ == "__main__":
    sys.exit(main())
