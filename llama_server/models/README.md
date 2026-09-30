# llama_server/models — Model Folders & Vision (mmproj) Rule

This is the **default model location** for this server (`llama_server/models/`).
Each subfolder holds ONE model. Drop files **directly into the model's own folder**,
flat, no nested subfolders — the scanner only reads `*.gguf` one level deep per folder.

> ⚠️ **VISION RULE (read this first):**
> To enable vision (image/video input) for a multimodal model, you MUST put its
> **vision projector file (`mmproj-*.gguf`, "mmproj" / "vision file") in the SAME
> folder as the main model `.gguf`**. Without it the server starts fine but image
> input fails with: `image input is not supported - hint: ... you may need to provide
> the mmproj`. The server auto-wires it on next (re)start — no config change needed.

## How the server finds the vision file (`model_discovery._find_mmproj_file`)

Search order for a given model file:

1. **The model's own folder** ← THIS is where you must put it
   - First *dedicated* match (name references the model, e.g. `mmproj-Qwen3.8-27B-bf16.gguf`,
     or prefix style `GLM-OCR.mmproj-Q8_0.gguf` next to `GLM-OCR.Q4_K_M.gguf`).
   - Then *generic* match: filename starts with `mmproj-` / `mmproj.` and contains only a
     quant tag (e.g. `mmproj-F16.gguf`, `mmproj-F32.gguf`). Priority: F16 > F32.
2. Default dir (`llama_server/models/`) — dedicated matches only, as a fallback.

Found → server starts llama-server with `--mmproj <file>`. Not found → `--mmproj-auto`
(llama.cpp guesses; usually finds nothing = **no vision**).

## Naming tips (so auto-detection works)

- Generic names work for any single model in the folder: `mmproj-F16.gguf`, `mmproj-F32.gguf`.
  ✅ e.g. a folder like `models/qwen36-27b-mtp/` that contains `mmproj-F16.gguf` gets vision automatically.
- Dedicated names are safest when a folder could hold several models:
  `mmproj-Qwen3.8-27B-bf16.gguf`, `GLM-OCR.mmproj-Q8_0.gguf`.
- Keep the model's family name in the mmproj filename (or use generic) — don't rename to
  something unrelated, or auto-detection won't match it.

## Where to get mmproj files for this setup

All links below were **byte-verified** (HTTP 206 + `GGUF` magic header) on 2026-08-23:

| Model | Vision file | Direct link | Size |
|---|---|---|---|
| Qwen3.8-27B-MTP (Q4_K_M…Q6_K, any quant) | `mmproj-F32.gguf` | https://huggingface.co/Jackrong/Qwen3.8-27B-MTP-GGUF/resolve/main/mmproj-F32.gguf | 1.84 GB (1,842,940,128 B) |
| Qwen3.8-27B (any quant) — smaller alternative | `mmproj-Qwen3.8-27B-bf16.gguf` | https://huggingface.co/bartowski/Qwen3.8-27B-GGUF/resolve/main/mmproj-Qwen3.8-27B-bf16.gguf | 0.93 GB (931,145,952 B) |
| Qwen3.6-27B | `mmproj-Qwen_Qwen3.6-27B-f16.gguf` | https://huggingface.co/bartowski/Qwen_Qwen3.6-27B-GGUF/resolve/main/mmproj-Qwen_Qwen3.6-27B-f16.gguf | 0.93 GB (884 MiB) — note: repo is `Qwen_Qwen3.6-27B-GGUF`, not `Qwen3.6-27B-GGUF` |
| GLM-OCR | `mmproj-GLM-OCR-Q8_0.gguf` | https://huggingface.co/ggml-org/GLM-OCR-GGUF/resolve/main/mmproj-GLM-OCR-Q8_0.gguf | 0.46 GB (461 MiB) — note: published by `ggml-org`, not unsloth |

⚠️ An mmproj is **model-specific** — never mix a Qwen projector with Gemma or another
family (the server's dedicated-match logic also refuses to force a generic one across
folders for exactly this reason).

## After placing the file

1. Put `mmproj-*.gguf` in the model folder (same level as the main GGUF).
2. Restart llama-server / switch models once (or let the stall auto-restart do it) —
   the next launch picks up `--mmproj` automatically.
3. Verify: logs show `Using DEDICATED mmproj ...` or `Using GENERIC mmproj ...`, and an
   image request no longer returns "image input is not supported".

## Folder layout example (vision enabled)

```
qwen38-27b-mtp/
├── README.md                      ← per-folder instructions
├── Qwen3.8-27B-MTP-Q6_K.gguf      ← main model
└── mmproj-F32.gguf                ← VISION FILE — same folder, required for vision
```
