# Model Folder: DeepSeek-R1-0528 (high-value reasoning model)

Second example folder in the default models location (`llama_server/models/`).
Drop the downloaded parts **directly into THIS folder** (flat, no subfolders —
the scanner only reads `*.gguf` one level deep).

> ## ⚠️ VISION RULE (applies to every model folder)
> For any **multimodal** model you add later, its vision projector file
> (`mmproj-*.gguf`, the "vision file") MUST be placed in the SAME folder as the main
> model `.gguf` — that's how the server auto-detects it and passes `--mmproj`. See
> `../README.md` for the full rule + download links.
> **DeepSeek-R1 is text-only, so this folder needs NO mmproj file** — vision simply does
> not exist for R1 (auto-detection falls back to `--mmproj-auto`, finds nothing, and that's fine).

## What to download

Model: **DeepSeek-R1-0528** — DeepSeek's flagship reasoning model (671B MoE, 37B active),
the open-source reference for deep chain-of-thought reasoning. Quantized by Unsloth
(Dynamic quants = best accuracy per bit). It is a *multi-part* GGUF: llama.cpp loads all
`-0000X-of-NNNNN.gguf` parts together automatically when you point `--model` at any one part.

### Source

Repo: **https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF**

Pick ONE quant tier and download ALL its numbered parts into this folder:

| Quant | Parts | Total size | Needs (RAM+VRAM) | Quality |
|---|---|---|---|---|
| **Q4_K_M** ← default pick here | 9 files | **~377 GiB (405 GB)** | ~480 GB RAM/VRAM combined | best balance, near-FP16 reasoning quality |
| UD-IQ1_S | 4 files | ~172 GiB | ~200 GB | runs on a 2×3090+RAM box; usable but noticeably dumber |
| IQ4_XS / Q5_K_M / Q8_0 … | see repo | larger | more | higher fidelity tiers in the same repo |

Q4_K_M direct links (all 9 parts, keep exact names):
```
https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF/resolve/main/Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00001-of-00009.gguf  (48.4 GB)
https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF/resolve/main/Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00002-of-00009.gguf  (49.5 GB)
https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF/resolve/main/Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00003-of-00009.gguf  (49.6 GB)
https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF/resolve/main/Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00004-of-00009.gguf  (48.3 GB)
https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF/resolve/main/Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00005-of-00009.gguf  (49.5 GB)
https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF/resolve/main/Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00006-of-00009.gguf  (48.3 GB)
https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF/resolve/main/Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00007-of-00009.gguf  (49.5 GB)
https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF/resolve/main/Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00008-of-00009.gguf  (47.0 GB)
https://huggingface.co/unsloth/DeepSeek-R1-0528-GGUF/resolve/main/Q4_K_M/DeepSeek-R1-0528-Q4_K_M-00009-of-00009.gguf  (14.8 GB)
```

## What goes into this folder

After download the folder should contain exactly:
```
deepseek-r1-0528/
├── README.md                      ← this file
└── DeepSeek-R1-0528-Q4_K_M-0000X-of-00009.gguf   (all 9 parts, names unchanged)
```
(No mmproj — R1 is text-only. Multimodal models in other folders DO need their
`mmproj-*.gguf` here next to the main GGUF.)

## How the server picks it up

- The scanner lists every `.gguf` part as a selectable model; when you load any one part,
  llama.cpp auto-loads its siblings (`-of-NNNNN` split convention).
- No "mtp" in the filename → MTP speculative decoding stays OFF for this model (correct —
  R1's MTP head is not included in these GGUFs; it runs as a standard model).
- No mmproj file → vision projector auto-detection falls back to `--mmproj-auto` and simply
  finds nothing (R1 is text-only, that's fine). For multimodal models the rule is different:
  put the `mmproj-*.gguf` in this same folder and it gets wired via `--mmproj` automatically.
- ⚠️ This folder must be reachable by the server: either add its full path to
  `allowed_models_folders` in `config/profiles.json`, or note that files placed directly in
  `llama_server/models/` are always scanned. (This subfolder works if added there.)

## Requirements / notes

- This is a **big-box model**: ~480 GB combined RAM+VRAM for Q4_K_M (e.g. dual RTX 3090/4090
  + 384–512 GB system RAM, or an EPYC box). It will NOT fit on the 24 GB desktop GPU that
  runs the Qwen3.8 folder — keep it for the server-class machine.
- R1 is a reasoning model: expect long thinking chains; raise `LLAMA_SERVER_MAX_NEW_TOKENS`
  and consider enabling `LLAMA_SERVER_REASONING` when using it, or send `/no_think` style
  prompts to skip thinking on simple tasks.
- Context: keep ctx moderate (8K–32K) — KV cache for a 671B MoE eats RAM fast.
