# Download llama_server files — Where to go, what to grab, where to put it

This folder (`local_ai/llama_server/`) is **excluded from git** (see `.gitignore`).
Everything in here must be downloaded manually on each machine. This file is the only
thing committed — it tells you exactly how to rebuild the folder.

The COOLEMS server auto-starts `llama-server.exe` from this folder
(`app/providers/llama/server.py`) and looks for models in `llama_server/models/` by default.

---

## 1. Where to go (source)

**Official pre-built binaries:** llama.cpp GitHub Releases page

- https://github.com/ggml-org/llama.cpp/releases
- Pinned build used by this codebase: **b10441** (2026, supports `--spec-type draft-mtp`
  for Qwen3.6 MTP models and the `--fit` / `--load-mode` flags) → release page:
  https://github.com/ggml-org/llama.cpp/releases/tag/b10441

If b10441 is ever removed, any **recent** Windows CUDA x64 build works — but check that it
still supports the flags in `config/config.py` (`--flash-attn`, `--cache-type-k/v`,
`--fit`, MTP speculative decoding).

## 2. What to download (Windows + NVIDIA GPU)

| Asset | Notes |
|---|---|
| `llama-b10441-bin-win-cuda-x64.zip` | Main package: `llama-server.exe` + all `ggml-*.dll`, CUDA kernels, bench/cli tools. **Required.** |
| `cudart-llama-bin-win-cuda*-x64.zip` (matching CUDA 12.x) | Only if your machine has no CUDA runtime installed yet — provides `cublas64_12.dll`, `cublasLt64_12.dll`, `cudart64_12.dll`. Skip if you already have a CUDA 12 toolkit / the DLLs are present. |
| CPU-only fallback: `llama-b10441-bin-win-cpu-x64.zip` | Only for machines without an NVIDIA GPU (slower, no flash-attn). |

> Pick the asset whose CUDA version matches your driver (this deployment runs on **CUDA 12** —
> that's why the folder contains `cublas*_12.dll` / `cudart64_12.dll`).

## 3. Where to put it

Extract the zip contents **directly into this folder**, so the layout is flat:

```
local_ai/llama_server/
├── download llama_server files.md   ← this file (the only git-tracked one)
├── llama-server.exe                 ← server.py expects EXACTLY this path/name
├── llama-common.dll
├── ggml.dll, ggml-base.dll, ggml-cpu-*.dll ...
├── ggml-cuda.dll                    ← CUDA backend (required for GPU offload)
├── cublas64_12.dll / cublasLt64_12.dll / cudart64_12.dll   (if from the cudart zip)
└── models/                          ← create this folder manually
    └── <your-model>.gguf            ← default model search dir
```

Rules:
- Do **not** extract into a nested subfolder — `server.py` hard-codes
  `<root>/llama_server/llama-server.exe`.
- All DLLs must sit next to the exe (flat layout).
- Create an empty `models/` folder even before you download a model.

## 4. Models (.gguf) — where to get them, where they go

Default config (`config/config.py`): `MODEL_NAME = "huihui_ai/Qwen3.6-abliterated:27b"`,
`LLAMA_URL = http://localhost:5000`.

- **Where:** Hugging Face → https://huggingface.co/huihui_ai (or the repo named in your
  profile's `allowed_models_folders`). Download a GGUF quant that fits your VRAM.
- **Where to put it:** `local_ai/llama_server/models/<file>.gguf` (default dir, scanned by
  `path_utils.py` / `model_discovery.py`). You can also point role profiles at other folders
  via `allowed_models_folders` in `profiles.json`.
- If the model is an MTP variant ("MTP" in the filename), `server.py` auto-enables
  speculative decoding (`--spec-type draft-mtp`) — needs a build that supports it (b10441+).
- Vision models: drop the matching `mmproj-*.gguf` next to the model file; the server is
  started with `--mmproj-auto`.

## 5. Verify the setup

```bat
cd local_ai\llama_server
.\llama-server.exe --version
```

Then start the SERVER (`ZZZ_SERVER.bat`). The provider auto-launches llama-server on port **5000**
with the flags from `config/config.py` (ctx 171072, all GPU layers, flash-attn, q8_0 KV cache)
and health-checks it at `http://localhost:5000`.

## 6. Expected file list after a correct install (b10441 win-cuda-x64 + CUDA 12 DLLs)

`llama-server.exe`, `llama-common.dll`, `ggml.dll`, `ggml-base.dll`,
`ggml-cpu-*.dll` (alderlake, cannonlake, cascadelake, cooperlake, haswell, icelake,
ivybridge, piledriver, sandybridge, sapphirerapids, skylakex, sse42, x64, zen4),
`ggml-cuda.dll`, `libomp140.x86_64.dll`, `cublas64_12.dll`, `cublasLt64_12.dll`,
`cudart64_12.dll`, plus the bench/cli tools (`llama-cli.exe`, `llama-bench.exe`, …).

Total size ≈ **1.3 GB** — that's why it lives outside git.
