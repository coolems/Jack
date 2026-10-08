"""Constants - verified 2026-09-19 against HuggingFace + GitHub APIs."""

MIN_PYTHON = (3, 10)

HF_MODEL_REPO = "unsloth/Qwen3.8-27B-GGUF"
HF_OCR_REPO = "ggml-org/GLM-OCR-GGUF"

# Vision projector (mmproj) for the main model - lives in the SAME HF repo as the GGUFs.
# Verified 2026-10-03 via the HF tree API; mmproj-F16.gguf is what our discovery code
# (_find_generic_mmproj) prefers when it sits NEXT TO the model file (the model's own
# folder is searched first), so init places it there.
HF_MMPROJ_FILE = "mmproj-F16.gguf"
MMPROJ_SIZE = 927_607_488

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
