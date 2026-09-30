# Model Folder: Qwen3.8-27B MTP (Multi-Token Prediction)

This folder is the **default model location** for this server (`llama_server/models/`).
Drop the model files described below **directly into THIS folder** (flat, no subfolders —
the scanner only reads `*.gguf` one level deep).

> ## ⚠️ VISION: put the vision file in THIS SAME FOLDER
> Qwen3.8-27B is a multimodal model, but **vision is OFF until you download its
> vision projector (mmproj) GGUF into this same folder**, next to the main model:
>
> ```
> qwen38-27b-mtp/
> ├── README.md                      ← this file
> ├── Qwen3.8-27B-MTP-Q4_K_M.gguf    ← main model (MTP head inside)
> └── mmproj-F32.gguf                ← VISION FILE — required for image/video input
> ```
>
> Download: https://huggingface.co/Jackrong/Qwen3.8-27B-MTP-GGUF/resolve/main/mmproj-F32.gguf (1.84 GB)
> Smaller alternative (0.93 GB): https://huggingface.co/bartowski/Qwen3.8-27B-GGUF/resolve/main/mmproj-Qwen3.8-27B-bf16.gguf
>
> Without it, image requests fail with `image input is not supported - hint: ... you may
> need to provide the mmproj`. Once the file sits here, the server auto-passes
> `--mmproj` on next (re)start — no config change needed. See `../README.md` for details.

## What to download

Model: **Qwen3.8-27B with MTP head baked in** (dense 27B vision-language model, native
262K context, thinking mode, tool calling). The "MTP" variant bundles the speculative
decoding head inside the same GGUF → ~1.9–2× faster generation at identical quality.

### Recommended source (single file, MTP included)

Repo: **https://huggingface.co/Jackrong/Qwen3.8-27B-MTP-GGUF**

| File | Size | Notes |
|---|---|---|
| `Qwen3.8-27B-MTP-Q4_K_M.gguf` | 16.8 GB (16,810,714,432 B) | **recommended default** — best quality/size balance for a 24 GB GPU |
| `mmproj-F32.gguf` | 1.8 GB (1,842,940,128 B) | **vision projector — REQUIRED in this folder to enable vision** |

Direct links:
- https://huggingface.co/Jackrong/Qwen3.8-27B-MTP-GGUF/resolve/main/Qwen3.8-27B-MTP-Q4_K_M.gguf
- https://huggingface.co/Jackrong/Qwen3.8-27B-MTP-GGUF/resolve/main/mmproj-F32.gguf

Other quants in the same repo if you have less/more VRAM:
`Q2_K` (10.9 GB), `IQ4_XS` (15.4 GB), `Q5_K_M` (19.5 GB), `Q6_K` (22.4 GB), `Q8_0` (29.0 GB).

### Alternative sources
- **ggml-org/Qwen3.8-27B-GGUF** — official conversion, but MTP head ships as a SEPARATE
  file (`mtp-Qwen3.8-27B-Q4_0.gguf`, 1.68 GB) that must be paired with the main GGUF via
  `--model-draft`. The Jackrong single-file variant above is simpler for this server.
- **bartowski/Qwen3.8-27B-GGUF** — MTP layers embedded in every quant (e.g.
  `Qwen3.8-27B-Q4_K_M.gguf`, 17.8 GB) + `mmproj-Qwen3.8-27B-bf16.gguf` (0.93 GB, half the
  size of F32 — put it in this same folder too).

## What goes into this folder

After download the folder should contain exactly:
```
qwen38-27b-mtp/
├── README.md                      ← this file
├── Qwen3.8-27B-MTP-Q4_K_M.gguf    ← main model (MTP head inside)
└── mmproj-F32.gguf                ← vision projector (REQUIRED for vision, same folder!)
```

## How the server picks it up (no config changes needed)

1. **Model scan** — `health.py` / `path_utils.py` glob this folder for `*.gguf`, skip any
   file with "mmproj" in the name, and list the rest as selectable models.
2. **MTP auto-detection** — `model_utils._is_mtp_model()` checks whether the filename
   contains "mtp". Because our file is named `Qwen3.8-27B-MTP-Q4_K_M.gguf`, the server
   automatically adds:
   ```
   --spec-type draft-mtp --spec-draft-n-max 4 ...
   ```
   (values from `config/config.py`: `LLAMA_SERVER_MTP_ENABLED=True`).
   ⚠️ Keep "MTP" in the filename — that's what triggers speculative decoding.
3. **Vision projector** — `model_discovery._find_mmproj_file()` looks in THIS folder first:
   dedicated name match (`mmproj-Qwen3.8-27B-bf16.gguf`) or generic (`mmproj-F32.gguf`,
   F16 > F32 priority). It then starts llama-server with `--mmproj <file>`. If the file is
   missing it falls back to `--mmproj-auto` → no vision.
4. **Startup model** — on fresh start the server loads the first GGUF found in this folder
   (`_resolve_startup_model`), i.e. this Qwen3.8 MTP file.

## Requirements / notes

- llama.cpp build must support `--spec-type draft-mtp` (MTP merged May 2026, PR #22673;
  repo requires b10419+). The bundled `llama_server/` binaries are recent enough.
- Recommended runtime settings for this model: `--spec-draft-n-max 2` is the publisher's
  tested value (our config uses 4 — fine, but drop to 2 if acceptance rate looks low in logs).
- VRAM: Q4_K_M + KV cache fits a 24 GB GPU with ~98K–130K context (measured on RTX 4090:
  19 GiB used at 98K ctx, MTP acceptance 71–100%).
- Thinking mode is ON by default in Qwen3.8; enable `LLAMA_SERVER_REASONING` in config if
  you want the server to pass `--reasoning on`.
