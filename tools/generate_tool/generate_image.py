"""Generate Image function - generates images via a GPU-adaptive diffusion pipeline (subprocess).

SELF-UNPACKING TOOL (2026-08-23)
================================
This tool is MINIMAL on the SERVER: this single .py file + a tiny tool_manifest.json
in its folder are all that ships to the CLIENT. The heavy runtime (a dedicated venv
with torch/diffusers, plus the worker script embedded below as WORKER_SOURCE) is
created ON THE USER MACHINE at first use under - INTERNAL to the client, never in the
user's working folder:

    <CLIENT_DIR>/tools/runtimes/generate_tool/
        image_worker.py          <- written from WORKER_SOURCE (hash-verified)
        requirements.txt         <- from TOOL_MANIFEST['requirements']
        .deps_installed          <- marker: pip install already ran for this venv
        venv/                    <- dedicated virtual env, REUSED across iterations
        hf_cache/                <- Hugging Face model weights (~10-30 GB), via HF_HOME

The unfold step is the GENERIC bootstrap in tools/tool_bootstrap.py (shared module,
delivered with every tool set): ensure_tool_runtime() is idempotent - an existing
healthy runtime from a previous iteration is detected and reused, so only the FIRST
generation pays setup cost. The same pattern applies to every future content-
generating tool (video generator etc.): minimal source on server, self-unfolded
runtime under <CLIENT_DIR>/tools/runtimes/<name>/ on the client machine. The CLIENT
publishes its own directory as the COOLEMS_CLIENT_ROOT environment variable; a legacy
<working_root>/tools/<name> runtime is migrated there once, automatically.

The diffusion model weights themselves are NOT shipped by us at all: they come from
Hugging Face (Apache 2.0, free for commercial use) - and since 2026-09-12 EVERY file
the tool needs lives in ONE place, inside its own runtime folder (see _tool_env /
_migrate_legacy_model_cache below):

    <CLIENT_DIR>/tools/runtimes/generate_tool/hf_cache/   <- model weights (5-80 GB per tier)

Both subprocesses this tool spawns run with HF_HOME pointed at that folder, so the
model never lands in the user's default %USERPROFILE%\\.cache\\huggingface. Deleting
the single runtime folder removes the whole tool (venv + weights); first use on a
fresh machine re-downloads everything into it. Since 2026-09-12 the tool also runs
snapshot_download in its own venv BEFORE generation so a first-run download gets its
own budget instead of dying inside IMAGE_SUBPROCESS_TIMEOUT - that was the cause of
endless 'timed out' loops on client PCs that did not share the server's HF cache.

LIVE MODEL-DOWNLOAD PROGRESS (2026-09-21)
=========================================
The pre-flight download now streams live progress: the prep script runs huggingface_hub
with a custom tqdm class (_PROGRESS_TQDM_SOURCE below) that prints one compact JSON line
per ~0.5 s per bar instead of redrawing terminal bars. hf 1.x's snapshot_download creates
exactly three bars from it - 'Fetching N files' (count), 'Downloading bytes' (network
aggregate, the speed source) and 'Reconstructing ...' (on-disk aggregate, whose total is
the true repo size): {"type":"count"|"total"|"phase",...}; per-file rows are emitted only
by non-snapshot hf paths. _ensure_model_cached() streams the script's stdout and merges
those events into <runtime_dir>/.setup_progress.json (stage 'model': current download,
files done/total, total size/downloaded/speed/ETA), written by the SAME SetupProgress
helper the venv/pip stages use in tools/tool_bootstrap.py. The MODEL_READY /
MODEL_NOT_READY line contract and the non-fatal semantics are UNCHANGED - progress is
informational only; a tracking failure can never break the download or generation.

GPU-ADAPTIVE TIERING (2026-09-12, SANA-Sprint v7 2026-09-13)
============================================================
At every call this tool PROBES THE PC FIRST (nvidia-smi, stdlib only) and picks the
model files + pipeline mode that fit the GPU - one tool works comfortably from a
6 GB laptop card to a 5090:

    high (>= 24 GB VRAM): Qwen/Qwen-Image in FP8 storage + cpu offload (v21, manifest-shipped)
                          - the strongest open image model in the lineup; ~50 FlowMatchEuler
                          steps, true_cfg_scale default 4.0. img2img loads the SEPARATE repo
                          Qwen/Qwen-Image-Edit via QwenImageEditPipeline (instruction-based).
    mid  (12-23.9 GB)   : Tongyi-MAI/Z-Image-Turbo in FP8 storage (~9 guidance-free steps;
                          ~17 GB peak resident; cpu offload auto-added below ~18 GB probed VRAM).
    low  (< 12 GB)      : int8-quantized SANA-Sprint 0.6B (v9) - ~5 GB download instead of
                          ~9.7 GB bf16; the worker dequantizes to the SAME bf16 tensors at load,
                          so VRAM/speed are unchanged (peak ~4.3 GB offloaded). Quality is a notch
                          below the 1.6B mid tier - the deliberate trade for the small download.
                          Until our int8 HF repo is published (_SANA_INT8_06_MODEL_ID starts with
                          'TODO') the low tier transparently runs the bf16 SANA-Sprint instead.


USER-DEFINED TIER OVERRIDE (2026-09-20)
=======================================
config/config.py IMAGE_GEN_TIER_OVERRIDE ('auto'|'high'|'mid'|'low', default 'low')
pins the model line INSTEAD of pure auto-classification - e.g. a 5090 user can run
the small SANA-Sprint line for seconds-fast images and a ~10 GB download instead of
Z-Image's ~23 GB resident pipeline. The value is delivered to the CLIENT via
config_constants (injected exec global; imported directly in local dev). SAFETY: if
the probed GPU cannot afford the pinned tier (see TIER_MIN_VRAM_MIB), the auto-
classified tier is used instead, so an override can never OOM a small card. Unknown
hardware (no NVIDIA GPU) always keeps the legacy 'high' path.

WHY SANA-SPRINT FOR THE SMALL TIERS (2026-09-13 field fix): the v6 table put laptop
cards on Z-Image-Turbo-FP8 + full cpu-offload, which streams an ~8 GB text encoder
over PCIe at EVERY image - minutes per picture on a 4080 laptop. SANA-Sprint is a
1.6B sCM-distilled model (2 steps, no guidance pass) that runs fully resident in
~11 GB: seconds-to-tens-of-seconds per image with zero offload traffic on mid-tier
cards, and only ~5.3 GB peak when offloaded for small cards. Z-Image-Turbo is a fixed
6B DiT - no smaller official variant exists - so it stays where the VRAM can afford it.

No NVIDIA GPU found -> legacy high path (unchanged).
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "Generate an image from a text prompt using the GPU-tier-appropriate diffusion pipeline (Qwen-Image, Z-Image or SANA-Sprint). Creates a PNG file in the working_root folder and returns the image data.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text description of the image to generate"
                },
                "height": {
                    "type": "integer",
                    "description": "Height of the image in pixels. Omit unless the user asks for a specific size - the default comes from the user's Settings > Runtime tab (Image Generator, default resolution 600x400; the worker snaps to tier-safe multiples - 32 on high (Qwen-Image) and low (SANA-Sprint), 16 on mid (Z-Image) - and clamps to the tier's max-pixel budget)"
                },
                "width": {
                    "type": "integer",
                    "description": "Width of the image in pixels. Omit unless the user asks for a specific size - the default comes from the user's Settings > Runtime tab (Image Generator, default resolution 600x400; the worker snaps to tier-safe multiples - 32 on high (Qwen-Image) and low (SANA-Sprint), 16 on mid (Z-Image) - and clamps to the tier's max-pixel budget)"
                },
                "num_inference_steps": {
                    "type": "integer",
                    "description": "Number of inference steps (default: 9)"
                },
                "guidance_scale": {
                    "type": "number",
                    "description": "Guidance scale for generation (default: 0.0)"
                },
                "output_filename": {
                    "type": "string",
                    "description": "Output filename (without extension, default: 'generated_image')"
                },
                "reference_image": {
                    "type": "string",
                    "description": "Optional: Path to a reference image for image-to-image generation. Must be inside working_root."
                },
                "strength": {
                    "type": "number",
                    "description": "Strength of transformation for img2img (0.0-1.0, default: 0.6)"
                }
            },
            "required": ["prompt"]
        }
    }
}

import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time as _time

# SERVER local-dev import. On the CLIENT this line is stripped by the loader and the
# constant arrives via tools_response 'config_constants' (injected into exec globals).
from config import IMAGE_GEN_TIER_OVERRIDE, IMAGE_SUBPROCESS_TIMEOUT  # noqa: F401

logger = logging.getLogger("COOLEMS.Tools.Generate")


# ---------------------------------------------------------------------------
# Embedded worker source (runs in the tool's OWN venv, never in this process).
# Single-quoted triple string: the worker uses only double-quoted docstrings.
# ---------------------------------------------------------------------------
WORKER_SOURCE = '''"""Image Generation Worker - runs in the self-unpacked tool venv subprocess.

Reads a JSON task from stdin, generates an image and writes the PNG into
working_root, printing a single JSON result line to stdout. The model is loaded
per invocation inside this dedicated subprocess and venv - it never touches the
server or the main LLM process.

GPU-ADAPTIVE LOADING (2026-09-12 / SANA-Sprint v7 2026-09-13, Qwen v21 2026-10-23): the task
carries 'pipeline' ('qwen_image'|'zimage'|'sana_sprint'), 'model_id', 'dtype'
('bf16'|'fp8'|'int8'), 'offload' and 'max_pixels' chosen by the caller after probing
the client GPU:

  * qwen_image (high tier, >= 24 GB, v21): Qwen/Qwen-Image via QwenImagePipeline in FP8
    storage + cpu offload - the strongest open image model in the lineup. It uses true
    CFG: guidance_scale <= 1 means 'use the repo default' (true_cfg_scale=4.0 with a
    negative prompt), and it needs ~50 FlowMatchEuler steps (the worker defaults to 50
    when the task carries no explicit step count). Dimensions snap to multiples of 32.
    img2img loads the SEPARATE repo Qwen/Qwen-Image-Edit via QwenImageEditPipeline -
    instruction-based editing with NO strength parameter (image + prompt only).
  * zimage (mid tier, 12-23.9 GB): ZImagePipeline / ZImageImg2ImgPipeline. dtype='fp8'
    keeps the transformer in FP8 e4m3fn STORAGE with bf16 compute via diffusers'
    enable_layerwise_casting() (>= 0.40) - half the VRAM of a resident bf16
    transformer, no Transformer Engine needed on any NVIDIA card.
  * sana_sprint (mid/low tiers): SanaSprintPipeline / SanaSprintImg2ImgPipeline -
    a 1.6B sCM-distilled model that runs in EXACTLY 2 steps at native 1024px. The
    SCM scheduler raises for any other step count, so the worker forces 2. It is
    guidance-distilled: it needs guidance_scale > 1 (the repo default is 4.5); a
    caller value of 0/<=1 means 'use the model default'. Mid tier runs fully
    resident (~11 GB peak, zero offload traffic - the fix for slow laptop cards);
        low tier adds enable_model_cpu_offload() and, since v8 (v9: the 0.6B build), loads
    OUR int8-quantized SANA-Sprint build (dtype='int8', _SANA_INT8_06_MODEL_ID): every
    nn.Linear weight arrives as true int8 + a per-channel scale sidecar on disk (~5 GB
    download vs 9.7 GB bf16). The worker loads it through the normal bf16 path and
    dequantizes in memory at load (_dequantize_int8_linears) - so VRAM/speed are exactly
    what a bf16 SANA-Sprint of that size would be; only the download is smaller. If the
    int8 repo is unavailable (unpublished / offline without cache) it falls back to the
    bf16 SANA-Sprint 0.6B so generation never breaks; the result JSON reports which one.
"""

import sys
import os
import json
import base64
import io
import argparse


def _get_working_dir(task):
    """Get the working directory from task.

    SECURITY (2026-07-15): there is NO fallback to a hardcoded project root.
    The caller MUST provide a valid, existing working_root; otherwise we raise
    so main() can emit an error JSON instead of writing outside it.
    """
    working_root = task.get("working_root")
    if not working_root or not os.path.isdir(working_root):
        raise ValueError(
            "Task is missing a valid 'working_root' directory; refusing to "
            f"write outside it (got: {working_root!r})."
        )
    return working_root


def _validate_filename(filename):
    """SECURITY (2026-07-15): reject output filenames with separators, traversal or absolute paths.

    Returns the sanitized basename to be used for the output file.
    """
    if filename is None:
        raise ValueError("Output filename must not be empty.")
    name = str(filename).strip()
    # Strip any directory part -- only the final basename is allowed
    name = os.path.basename(name.replace("\\\\", "/"))
    if not name or name in (".", ".."):
        raise ValueError(f"Invalid output filename: {filename!r}")
    for bad in ("/", "\\\\", ".."):
        if bad in name:
            raise ValueError(f"Invalid output filename (contains {bad!r}): {name!r}")
    return name


def _clamp_resolution(task, snap=32):
    """Clamp width/height to a tier-safe multiple (snap), then to the max_pixels budget.

    Returns (width, height) actually used for generation. Every pipeline this tool can
    load validates its resolution BEFORE generating: Z-Image (mid tier) requires both
    dimensions divisible by vae_scale_factor * 2 = 16, SANA-Sprint (low tier) requires
    divisibility by 32, and Qwen-Image (high tier) requires 32 as well - so the caller
    passes snap=16 only for Z-Image and snap=32 for qwen_image / sana_sprint. The user's
    dimensions are snapped to the nearest LOWER multiple of snap FIRST; the max_pixels
    budget loop below then only ever subtracts whole snaps, so the returned pair can
    never break that validation. (2026-10-05: mid tier no longer forced to multiples of
    32 - Z-Image accepts any multiple of 16. v21: the high tier moved to Qwen/Qwen-Image,
    which requires multiples of 32 and snaps like SANA.)
    """
    width = int(task.get("width", 600)) // snap * snap
    height = int(task.get("height", 400)) // snap * snap
    max_pixels = task.get("max_pixels")
    if not max_pixels or max_pixels <= 0:
        return max(width, snap), max(height, snap)
    target = float(max_pixels)
    while width * height > target and (width > snap or height > snap):
        if width >= height and width > snap:
            width -= snap
        elif height > snap:
            height -= snap
    return max(width, snap), max(height, snap)


def _sana_steps():
    """SANA-Sprint is sCM-distilled for EXACTLY 2 steps.

    The SCM scheduler only supports its intermediate_timestep schedule at 2 steps
    (any other count raises ValueError unless custom timesteps are supplied), and
    the model's quality is tuned for that 2-step trajectory - so user-supplied
    step counts are ignored for this pipeline.
    """
    return 2


def _sana_guidance(task):
    """Guidance scale for SANA-Sprint (guidance-distilled: it needs > 1).

    The repo default is 4.5. A caller value of 0 / missing / <= 1 means 'use the
    model default'; a larger explicit value is honored as-is.
    """
    try:
        gs = float(task.get("guidance_scale") or 0.0)
    except (TypeError, ValueError):
        gs = 0.0
    return gs if gs > 1.0 else 4.5


def _int8_snapshot_dir(model_id):
    """Local snapshot dir of the int8 build (HF_HOME cache), or None when unavailable."""
    try:
        from huggingface_hub import snapshot_download
        return snapshot_download(model_id, local_files_only=True)
    except Exception:
        try:
            from huggingface_hub import hf_hub_download
            p = hf_hub_download(repo_id=model_id, filename="model_index.json")
            return os.path.dirname(p)
        except Exception:
            return None


def _dequantize_int8_linears(pipe, model_id):
    """Dequantize an int8 SANA-Sprint build (low tier, v8/v9) to bf16 in memory.

    The build stores every nn.Linear weight as true int8 plus one per-channel float32
    scale in <component>/scales.safetensors; all other params (norms/embeddings/VAE)
    are plain bf16, unchanged from the original repo. Loading went through the normal
    bf16 path, so each Linear weight currently HOLDS ITS INTEGER VALUE in a float
    dtype - this rebuilds it as w = w_int * scale (bf16). Result: exactly the tensors
    an int8-quantized model is supposed to produce (same VRAM/speed as bf16), only
    the download was smaller. The sidecar's key set is authoritative: every listed
    weight must be found and transformed, otherwise the build is corrupt and we raise
    loudly instead of generating garbage images. Returns dequantized count.
    """
    import os

    import torch
    from safetensors.torch import load_file

    snap = _int8_snapshot_dir(model_id)
    if not snap:
        raise RuntimeError("cannot locate the int8 snapshot dir (scales sidecar)")
    total = 0
    for sub in ("text_encoder", "transformer"):
        comp = getattr(pipe, sub, None)
        if comp is None:
            continue
        spath = os.path.join(snap, sub, "scales.safetensors")
        if not os.path.isfile(spath):
            continue  # component has no sidecar (e.g. VAE) - nothing to dequantize
        scales = load_file(spath, device="cpu")
        scale_keys = {k[: -len(".scale")] for k in scales.keys() if k.endswith(".scale")}
        done = 0
        for cname, mod in list(comp.named_modules()):
            key = (cname + ".weight") if cname else "weight"
            if key not in scale_keys:
                continue
            w = getattr(mod, "weight", None)
            if not isinstance(w, torch.nn.Parameter):
                raise RuntimeError(f"int8 build: expected a weight parameter at {key}")
            scale = scales[key + ".scale"]
            with torch.no_grad():
                mod.weight = torch.nn.Parameter(
                    (w.to(torch.float32) * scale.unsqueeze(1)).to(torch.bfloat16),
                    requires_grad=False)
            done += 1
        if done != len(scale_keys):
            raise RuntimeError(f"int8 build: dequantized {done}/{len(scale_keys)} "
                               f"Linear weights in {sub} - corrupt sidecar?")
        total += done
    if total == 0:
        raise RuntimeError("int8 build produced no scales sidecar - nothing was dequantized")
    return total
def _load_pipeline(pipeline_cls, task):
    """Load the tier-appropriate pipeline (model/dtype/offload come from the task).

    Local-first: an already-cached snapshot loads with local_files_only=True in a
    couple of seconds; only when the cache is missing/incomplete does it fall back
    to downloading - inside this worker's own subprocess budget, which per tier is
    generous enough for a first-run ~10-30 GB download (2026-09-12 client fix).

    v8/v9 int8 low tier: dtype='int8' loads OUR quantized SANA-Sprint build through the
    normal bf16 path and dequantizes it in memory. If that repo is unavailable (not
    published yet, or offline with no local cache) it transparently falls back to the
    bf16 SANA-Sprint 0.6B so generation never breaks. Returns (pipe, loaded_model_id,
    loaded_dtype) - the caller reports which files actually ran.
    """
    import torch

    model_id = task.get("model_id") or "Tongyi-MAI/Z-Image-Turbo"
    dtype_name = str(task.get("dtype", "bf16")).lower()
    offload = bool(task.get("offload"))
    loaded_model_id, loaded_dtype = model_id, dtype_name

    if dtype_name == "int8":
        try:
            pipe = pipeline_cls.from_pretrained(model_id, torch_dtype=torch.bfloat16,
                                                local_files_only=True)
        except Exception:
            try:
                pipe = pipeline_cls.from_pretrained(model_id, torch_dtype=torch.bfloat16)
            except Exception as e:
                # Int8 build unavailable (unpublished / offline without cache): fall
                # back to the bf16 SANA-Sprint 0.6B - same model class as this tier's int8 build (v9).
                fb = "Efficient-Large-Model/Sana_Sprint_0.6B_1024px_diffusers"
                import logging as _lg
                _lg.getLogger("worker").warning(
                    f"int8 SANA-Sprint unavailable ({str(e)[:160]}) - falling back to bf16")
                try:
                    pipe = pipeline_cls.from_pretrained(fb, torch_dtype=torch.bfloat16,
                                                        local_files_only=True)
                except Exception:
                    pipe = pipeline_cls.from_pretrained(fb, torch_dtype=torch.bfloat16)
                loaded_model_id, loaded_dtype = fb, "bf16"
        if loaded_dtype == "int8":
            _dequantize_int8_linears(pipe, model_id)
    else:
        try:
            pipe = pipeline_cls.from_pretrained(model_id, torch_dtype=torch.bfloat16,
                                                local_files_only=True)
        except Exception:
            pipe = pipeline_cls.from_pretrained(model_id, torch_dtype=torch.bfloat16)

    # fp8 tier (v21: Qwen high tier + Z-Image mid tier): keep the transformer in FP8
    # e4m3fn STORAGE with bf16 compute via diffusers' layerwise casting (>= 0.40). The
    # upcast happens per-layer on the fly - half the VRAM of a resident bf16, no
    # Transformer Engine needed on any NVIDIA card. Precision-critical layers
    # (norms/embeddings/projections) stay in the compute dtype by default, so quality is
    # near-bf16. Loading starts from bf16 so F8_E4M3 checkpoint tensors are cast to a
    # usable module dtype first; layerwise casting then shrinks storage back to fp8.
    if dtype_name == "fp8":
        try:
            pipe.transformer.enable_layerwise_casting(
                storage_dtype=torch.float8_e4m3fn, compute_dtype=torch.bfloat16)
        except Exception as e:
            # Older diffusers without layerwise casting: degrade to plain bf16.
            import logging as _lg
            _lg.getLogger("worker").warning(f"layerwise fp8 unavailable ({e}); using bf16")

    if offload and torch.cuda.is_available():
        # Stream the big components (transformer / text encoder) from CPU RAM to the
        # GPU one at a time - peak VRAM ~4-6 GB (0.6B/1.6B int8 builds) vs ~11 GB resident.
        pipe.enable_model_cpu_offload()
    else:
        pipe.to("cuda" if torch.cuda.is_available() else "cpu")
    return pipe, loaded_model_id, loaded_dtype


def _save_and_encode(task, image):
    """Save *image* inside working_root and return the ok-result dict."""
    working_dir = _get_working_dir(task)
    safe_name = _validate_filename(task.get("output_filename", "generated_image"))
    os.makedirs(working_dir, exist_ok=True)
    filepath = os.path.normpath(os.path.join(working_dir, f"{safe_name}.png"))

    # SECURITY (2026-07-15): final containment check -- the output file must
    # stay inside working_root (realpath resolves symlinks/..).
    real_file = os.path.realpath(filepath)
    real_wr = os.path.realpath(working_dir)
    if not (real_file == real_wr or real_file.startswith(real_wr + os.sep)):
        raise ValueError(f"Output path escapes working_root: {filepath}")

    image.save(filepath)

    buffered = io.BytesIO()
    image.save(buffered, format="PNG")
    img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return {
        "status": "ok",
        "filename": f"{safe_name}.png",
        "file_path": filepath,
        "image_base64": img_base64,
        "width": task.get("_used_width"),
        "height": task.get("_used_height"),
    }


def _qwen_cfg_scale(task):
    """True-CFG scale for Qwen/Qwen-Image (v21 high tier).

    The repo default is true_cfg_scale=4.0 with a negative prompt. A caller value of
    0 / missing / <= 1 means 'use the model default'; a larger explicit value is
    honored as-is - same convention SANA-Sprint's guidance helper uses.
    """
    try:
        gs = float(task.get("guidance_scale") or 0.0)
    except (TypeError, ValueError):
        gs = 0.0
    return gs if gs > 1.0 else 4.0


def generate_text_to_image(task):
    """Generate image from text prompt (pipeline chosen by the GPU tier)."""
    pipeline_name = str(task.get("pipeline") or "zimage").lower()

    prompt = task["prompt"]
    # Tier-aware snap (2026-10-05, v21): Qwen-Image (high) and SANA-Sprint (low) require
    # multiples of 32; Z-Image (mid) validates multiples of 16.
    width, height = _clamp_resolution(task, snap=32 if pipeline_name in ("qwen_image", "sana_sprint") else 16)
    task["_used_width"], task["_used_height"] = width, height

    if pipeline_name == "qwen_image":
        from diffusers import QwenImagePipeline

        # v21 high tier: ~50 FlowMatchEuler steps is the model's tuned trajectory; an
        # explicit caller step count (task carries one) always wins.
        num_inference_steps = task.get("num_inference_steps") or 50
        pipe, lm_id, ld_ty = _load_pipeline(QwenImagePipeline, task)
        task["_loaded_model_id"], task["_loaded_dtype"] = lm_id, ld_ty
        image = pipe(
            prompt=prompt,
            negative_prompt="",
            true_cfg_scale=_qwen_cfg_scale(task),
            height=height,
            width=width,
            num_inference_steps=num_inference_steps,
        ).images[0]

    elif pipeline_name == "sana_sprint":
        from diffusers import SanaSprintPipeline

        # 2-step sCM-distilled model at native 1024px - the tier's max_pixels clamp
        # keeps us inside its trained resolution range. v9: low tier may be OUR int8
        # SANA-Sprint 0.6B build, dequantized to bf16 at load (see _load_pipeline).
        pipe, lm_id, ld_ty = _load_pipeline(SanaSprintPipeline, task)
        task["_loaded_model_id"], task["_loaded_dtype"] = lm_id, ld_ty
        image = pipe(
            prompt=prompt,
            height=height,
            width=width,
            num_inference_steps=_sana_steps(),
            guidance_scale=_sana_guidance(task),
        ).images[0]
    elif pipeline_name in ("zimage", "z_image"):
        from diffusers import ZImagePipeline

        num_inference_steps = task.get("num_inference_steps", 9)
        guidance_scale = task.get("guidance_scale", 0.0)

        # Load the tier-appropriate model (HF cache inside <runtime_dir>/hf_cache).
        pipe, lm_id, ld_ty = _load_pipeline(ZImagePipeline, task)
        task["_loaded_model_id"], task["_loaded_dtype"] = lm_id, ld_ty
        image = pipe(
            prompt=prompt,
            height=height,
            width=width,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
        ).images[0]
    else:
        # A manifest naming a pipeline this worker does not know must fail LOUDLY - the
        # old catch-all silently rendered with Z-Image even when the tier asked for
        # something else (2026-10-23: that is how the Qwen remap crashed with a FrozenDict
        # error instead of a clear message).
        raise ValueError(f"Unknown pipeline {pipeline_name!r} for text2img "
                         f"(this worker supports qwen_image, sana_sprint, zimage)")

    result = _save_and_encode(task, image)
    result["message"] = "Image generated successfully"
    return result


def _qwen_edit_task(task):
    """A copy of the task re-pointed at the Edit repo (img2img high tier, v21).

    Qwen-Image-Edit is a SEPARATE repo/architecture from Qwen-Image, so the pipeline
    must be loaded from `edit_model_id` while every other task field (dtype/offload/
    max_pixels) stays as the tier configured it. Falling back to the built-in id keeps
    a tier table that predates `edit_model_id` working instead of loading the base
    model into QwenImageEditPipeline (which fails on a missing component).
    """
    t = dict(task)
    t["model_id"] = str(t.get("edit_model_id") or "").strip() or _QWEN_EDIT_MODEL_ID
    return t


def generate_image_to_image(task):
    """Generate image from reference image + prompt (pipeline chosen by the GPU tier)."""
    import torch
    from diffusers.utils import load_image

    pipeline_name = str(task.get("pipeline") or "zimage").lower()

    prompt = task["prompt"]
    reference_image_path = task["reference_image"]
    # Tier-aware snap (2026-10-05, v21): Qwen-Image (high) and SANA-Sprint (low) require
    # multiples of 32; Z-Image (mid) validates multiples of 16.
    width, height = _clamp_resolution(task, snap=32 if pipeline_name in ("qwen_image", "sana_sprint") else 16)
    task["_used_width"], task["_used_height"] = width, height
    strength = task.get("strength", 0.6)

    # Load reference image at the (clamped) target resolution.
    init_image = load_image(reference_image_path).resize((width, height))
    device = "cuda" if torch.cuda.is_available() else "cpu"

    if pipeline_name == "qwen_image":
        # v21 high tier img2img: the SEPARATE Edit repo - instruction-based editing.
        # QwenImageEditPipeline has NO strength parameter (image + prompt only), so
        # task["strength"] is intentionally ignored on this path.
        from diffusers import QwenImageEditPipeline

        num_inference_steps = task.get("num_inference_steps") or 50
        pipe, lm_id, ld_ty = _load_pipeline(QwenImageEditPipeline, _qwen_edit_task(task))
        task["_loaded_model_id"], task["_loaded_dtype"] = lm_id, ld_ty
        image = pipe(
            image=init_image,
            prompt=prompt,
            negative_prompt="",
            true_cfg_scale=_qwen_cfg_scale(task),
            height=height,
            width=width,
            num_inference_steps=num_inference_steps,
        ).images[0]

    elif pipeline_name == "sana_sprint":
        from diffusers import SanaSprintImg2ImgPipeline

        pipe, lm_id, ld_ty = _load_pipeline(SanaSprintImg2ImgPipeline, task)
        task["_loaded_model_id"], task["_loaded_dtype"] = lm_id, ld_ty
        image = pipe(
            prompt=prompt,
            image=init_image,
            strength=strength,
            num_inference_steps=_sana_steps(),
            guidance_scale=_sana_guidance(task),
            generator=torch.Generator(device).manual_seed(42),
        ).images[0]
    elif pipeline_name in ("zimage", "z_image"):
        from diffusers import ZImageImg2ImgPipeline

        num_inference_steps = task.get("num_inference_steps", 9)
        guidance_scale = task.get("guidance_scale", 0.0)

        pipe, lm_id, ld_ty = _load_pipeline(ZImageImg2ImgPipeline, task)
        task["_loaded_model_id"], task["_loaded_dtype"] = lm_id, ld_ty
        image = pipe(
            prompt=prompt,
            image=init_image,
            strength=strength,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            generator=torch.Generator(device).manual_seed(42),
        ).images[0]
    else:
        raise ValueError(f"Unknown pipeline {pipeline_name!r} for img2img "
                         f"(this worker supports qwen_image, sana_sprint, zimage)")

    result = _save_and_encode(task, image)
    result["message"] = "Image transformed successfully"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["text2img", "img2img"], required=True)
    args, remaining = parser.parse_known_args()

    # Read task from stdin.
    try:
        task = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "message": f"Invalid JSON on stdin: {e}"}))
        sys.exit(1)

    try:
        if args.mode == "text2img":
            result = generate_text_to_image(task)
        elif args.mode == "img2img":
            result = generate_image_to_image(task)
        else:
            result = {"status": "error", "message": f"Unknown mode: {args.mode}"}

        # Report the tier choice so logs show which pipeline ran.
        if isinstance(result, dict):
            result["pipeline"] = task.get("pipeline") or "zimage"
            # v8: report what ACTUALLY loaded (the int8 build, or the bf16 fallback).
            result["model_id"] = task.get("_loaded_model_id") or task.get("model_id")
            result["dtype"] = task.get("_loaded_dtype") or task.get("dtype")
            if str(result.get("dtype")) == "int8":
                result["model_loaded"] = "int8"
            elif str(task.get("pipeline")) == "sana_sprint":
                result["model_loaded"] = "bf16"
            result["offload"] = bool(task.get("offload"))

        # Output result as JSON to stdout.
        print(json.dumps(result))
    except Exception as e:
        import traceback
        error_msg = f"{str(e)}\\n{traceback.format_exc()}"
        print(json.dumps({"status": "error", "message": error_msg}))
        sys.exit(1)


if __name__ == "__main__":
    main()
'''

# ---------------------------------------------------------------------------
# Tool manifest (mirrors tools/generate_tool/tool_manifest.json on the SERVER). The SERVER ships its copy
# with tool_code_response; the loader injects it into exec globals as TOOL_MANIFEST, which
# _active_manifest() prefers. This in-file dict is only the FALLBACK for a legacy server
# that does not ship one.

_DEFAULT_MANIFEST = {
        "name": "generate_image",
        "version": 23,  # mirrors tools/generate_tool/tool_manifest.json - only used when a legacy SERVER ships no TOOL_MANIFEST at all
        "runtime_name": "generate_tool",
        "requirements": ["torch==2.9.1+cu128", "diffusers>=0.40", "transformers", "accelerate>=0.17.0"],
        "extra_index_urls": ["https://download.pytorch.org/whl/cu128"],
    }

_WORKER_FILENAME = "image_worker.py"


# ---------------------------------------------------------------------------
# Model repos (Hugging Face, Apache 2.0 / free for commercial use). The GPU tier
# table below picks which one each machine downloads and loads.
# ---------------------------------------------------------------------------

#: v21 high-tier model - Qwen/Qwen-Image (20B MMDiT + Qwen2.5-VL-7B encoder), the strongest
#: open image model in the lineup. Runs FP8-stored with cpu offload on >= 24 GB cards;
#: img2img uses the separate Edit repo below via QwenImageEditPipeline (instruction-based).
_QWEN_MODEL_ID = "Qwen/Qwen-Image"

#: v21 high-tier img2img model - instruction-based editing, NO strength parameter.
_QWEN_EDIT_MODEL_ID = "Qwen/Qwen-Image-Edit"

#: Mid-tier model (v21) - Z-Image-Turbo in FP8 storage (~9 guidance-free steps). Pre-v21 it
#: was the high tier's bf16 resident line; v7 kept SANA-Sprint for mid only until then.
_MODEL_ID = "Tongyi-MAI/Z-Image-Turbo"

#: v7 mid/low-tier model - NVIDIA SANA-Sprint 1.6B, sCM-distilled to EXACTLY 2 steps
#: at native 1024px (~9.7 GB download; ~11 GB peak resident, ~5.3 GB offloaded).
_SANA_MODEL_ID = "Efficient-Large-Model/Sana_Sprint_1.6B_1024px_diffusers"


#: v8 int8 build of SANA-Sprint 1.6B (built with scripts/quantize_sana_int8.py from the
#: Apache-2.0 bf16 repo above; ~6 GB). Kept in KNOWN_MODEL_IDS so a machine that already
#: cached it is never migrated away - but v9 moved the LOW tier to the 0.6B build below:
#: the user wanted the smallest possible low-tier download, and 0.6B int8 lands at ~5 GB.
_SANA_INT8_16_MODEL_ID = "TODO/publish-then-set-owner/Sana_Sprint_1.6B_int8"

#: v9 LOW-TIER model - OUR int8 build of SANA-Sprint 0.6B (scripts/quantize_sana_int8.py
#: --small, from the Apache-2.0 bf16 repo below). Every nn.Linear weight is stored as true
#: int8 + a per-channel scale sidecar: ~5 GB total download (Gemma encoder int8 ~3.2 GB +
#: 0.6B transformer int8 ~0.6 GB + VAE bf16 1.25 GB, which is the hard floor) vs 9.7 GB for
#: the bf16 1.6B repo. Generation runs on exactly the same bf16 tensors (the worker
#: dequantizes at load - see _dequantize_int8_linears in WORKER_SOURCE); quality sits a notch
#: below the 1.6B mid tier, which is the deliberate trade for the small download.
#: PUBLISH GATE: until this folder is published as an HF model repo and its id set here,
#: the placeholder below keeps the low tier on the bf16 SANA-Sprint via _int8_model_ready()
#: - generation never breaks; it just downloads 9.7 GB instead of ~5 GB.
_SANA_INT8_06_MODEL_ID = "TODO/publish-then-set-owner/Sana_Sprint_0.6B_int8"

#: v7 low-tier fallback model (also the worker's last-resort int8 fallback) - NVIDIA's
#: bf16 SANA-Sprint 0.6B: ~7.7 GB download, same pipeline/speed as the mid tier.
_SANA_06_MODEL_ID = "Efficient-Large-Model/Sana_Sprint_0.6B_1024px_diffusers"


def _int8_model_ready(model_id: str) -> bool:
    """True when an int8 SANA-Sprint build is published and configured (v8/v9).

    The placeholder id starts with 'TODO' - any such value means 'not published yet',
    so the low tier falls back to bf16 instead of trying to download a repo that does
    not exist. A real owner/name id passes this check.
    """
    mid = str(model_id or "").strip()
    return bool(mid) and "/" in mid and not mid.startswith("TODO")


# ---------------------------------------------------------------------------
# GPU-adaptive tiering (2026-09-12 / SANA-Sprint v7 2026-09-13): probe the client PC,
# then pick model files + pipeline mode so generation is comfortable from a 6 GB
# laptop card to a 5090.
# ---------------------------------------------------------------------------

#: Every repo this tool may cache - _migrate_legacy_model_cache must never move away
#: a cache that already holds ANY of these (e.g. an FP8 or SANA variant).
KNOWN_MODEL_IDS = [
        _QWEN_MODEL_ID,
        _QWEN_EDIT_MODEL_ID,
        "Tongyi-MAI/Z-Image-Turbo",
        "ykarout/Z-Image-Turbo-FP8-Full",
        _SANA_MODEL_ID,
        _SANA_06_MODEL_ID,
        _SANA_INT8_16_MODEL_ID,  # v8 build: only meaningful once published (see constants)
        _SANA_INT8_06_MODEL_ID,  # v9 low-tier build: same gate
    ]

# Tier -> pipeline choice. VRAM thresholds follow MEASURED peak usage (2026-09-13):
#   * Z-Image bf16 resident peaks ~23 GB  -> only for >= 24 GB cards (5090/A6000).
#     (v6 put 16 GB cards here - they would have OOM'd; fixed in v7.)
#   * SANA-Sprint resident @<=1024px peaks ~11 GB -> fits any >= 12 GB card
#     (laptop 4080, desktop 3070/3060 12GB, laptop 4090) with headroom.
#   * SANA-Sprint + cpu offload peaks ~5.3 GB -> any smaller card; resolution clamp
#     keeps activations small (laptop 4070 8GB, laptop 3060 6GB).
TIER_MODELS = {
    "high": {
        # >= 24 GB (v21): Qwen/Qwen-Image FP8 storage + cpu offload - the strongest open
        # model in the lineup; img2img via the separate Edit repo (edit_model_id).
        "model_id": _QWEN_MODEL_ID,
        "edit_model_id": _QWEN_EDIT_MODEL_ID,
        "dtype": "fp8",
        "offload": True,
        "max_pixels": 1024 * 1024,
        "pipeline": "qwen_image",
    },
    "mid": {
        # 12-23.9 GB (v21): Z-Image-Turbo in FP8 storage - ~9 guidance-free steps; fp8 halves
        # transformer storage (~17 GB peak resident). On probed VRAM below ~18 GB the caller
        # adds cpu offload so 12/16 GB cards run it without OOM.
        "model_id": _MODEL_ID,
        "dtype": "fp8",
        "offload": False,
        "max_pixels": 1024 * 1024,
        "pipeline": "zimage",
    },
    "low": {
        # < 12 GB: int8 SANA-Sprint 0.6B (v9) - ~5 GB download instead of ~9.7 GB bf16; the
        # worker dequantizes to the SAME bf16 tensors, so peak VRAM (~4.3 GB offloaded) and
        # speed are unchanged; quality is a notch below the 1.6B mid tier (the trade). While
        # _SANA_INT8_06_MODEL_ID is still the TODO placeholder (repo unpublished)
        # generate_image() swaps in the bf16 SANA-Sprint 0.6B model here (~7.7 GB).
        "model_id": _SANA_INT8_06_MODEL_ID,
        "dtype": "int8",
        "offload": True,
        "max_pixels": 768 * 512,
        "pipeline": "sana_sprint",
    },
}

#: Fallback budget (s) for offloaded first runs when the server constant is absent.
#: v21: raised 900 -> 1800 - the high tier's Qwen/Qwen-Image loads a ~40 GB FP8-stored
#: transformer through cpu offload and then runs ~50 FlowMatchEuler steps; first warm on a
#: cold page cache can exceed 900 s even on good hardware.
_OFFLOAD_TIMEOUT_FALLBACK = 1800


def _probe_gpu() -> dict:
    """Probe the client's NVIDIA GPU(s) via nvidia-smi (stdlib only - no torch).

    Returns {'name', 'vram_mib', 'compute_cap'} for the GPU with the MOST total VRAM
    (multi-GPU machines pick the biggest), or {} when nvidia-smi is missing, fails or
    reports nothing usable. Callers treat an empty result as 'unknown hardware' and
    keep the legacy high-tier behavior - probing must never break generation.
    """
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
    except Exception as e:
        logger.debug(f"  GPU probe failed (nvidia-smi): {e}")
        return {}
    best = None
    for line in (r.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        name = parts[0]
        try:
            vram_mib = int(float(parts[1]))
        except ValueError:
            continue
        compute_cap = parts[2].strip() if len(parts) > 2 else ""
        if vram_mib <= 0:
            continue
        if best is None or vram_mib > best["vram_mib"]:
            best = {"name": name, "vram_mib": vram_mib, "compute_cap": compute_cap}
    return best or {}


def _classify_tier(gpu_info: dict) -> str:
    """Map probed VRAM to a tier (boundaries from measured peak usage - see TIER_MODELS).

    Unknown hardware -> 'high' (the pre-tiering legacy path is preserved exactly).
    """
    if not gpu_info:
        return "high"
    vram_mib = int(gpu_info.get("vram_mib") or 0)
    if vram_mib >= 24 * 1024:
        return "high"
    if vram_mib >= 12 * 1024:
        return "mid"
    return "low"


#: Minimum VRAM (MiB) a card needs to run each tier WITHOUT OOMing - measured peak usage.
#: Used only as the SAFETY NET for IMAGE_GEN_TIER_OVERRIDE: if the user pins a tier their
#: GPU cannot afford, generate_image keeps the auto-classified tier instead of crashing.
TIER_MIN_VRAM_MIB = {
    "high": 24 * 1024,   # v21 Qwen/Qwen-Image fp8 + cpu offload (20B MMDiT + 7B encoder)
    "mid": 12 * 1024,    # v21 Z-Image-Turbo fp8 resident peaks ~17 GB; offloaded below ~18 GB
    "low": 0,            # SANA-Sprint offloaded - fits any card (peak ~4.3-5.3 GB)
}


def _load_client_settings() -> dict:
    """The CLIENT's config/settings.json as a dict, or {} when unavailable.

    (2026-10-03) Shared by the Runtime-tab readers below (_client_image_gen_tier and
    _client_image_gen_resolution). Path resolution: 1. <CWD>/config/settings.json -
    normal CLIENT run (CWD = CLIENT dir); 2. <local_ai>/CLIENT/config/settings.json -
    SERVER-side layout. A read problem must never break generation.
    """
    try:
        candidates = [os.path.join(os.getcwd(), "config", "settings.json")]
        try:
            module_dir = os.path.dirname(os.path.abspath(__file__))
            local_ai_root = os.path.dirname(os.path.dirname(module_dir))  # generate_tool -> tools -> local_ai
            candidates.append(os.path.join(local_ai_root, "CLIENT", "config", "settings.json"))
        except Exception:
            pass

        for candidate in candidates:
            try:
                if os.path.isfile(candidate):
                    with open(candidate, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"  Could not read CLIENT settings.json: {e}")
    return {}


def _client_image_gen_tier() -> str:
    """The per-machine tier pick from the CLIENT Settings UI (2026-10-03), or ''.

    The Runtime tab writes 'image_gen_tier' into <CLIENT>/config/settings.json via
    POST /api/settings. This reads that file LIVE on every call - same live-read
    pattern web_search.py uses for search_engines.json - so a pick in the UI applies
    to the very next generation without any restart or SERVER round-trip.

    Returns '' when the key is absent or not a known tier name, so callers fall back
    to the SERVER-shipped constant below. A read problem must never break generation.
    """
    data = _load_client_settings()
    if not data:
        return ""
    tier = str(data.get("image_gen_tier") or "").strip().lower()
    return tier if tier in ("auto", "high", "mid", "low") else ""


def _client_image_gen_resolution():
    """(width, height) the user set as DEFAULT in Settings > Runtime, or None.

    (2026-10-03) The Runtime tab stores 'image_gen_width' / 'image_gen_height' (px,
    32..4096, validated by POST /api/settings) in <CLIENT>/config/settings.json. Read
    LIVE on every call - a save applies to the very next generation without restart.
    Returns None for any missing/invalid value so callers keep their own defaults
    (600x400). A read problem must never break generation.
    """
    try:
        data = _load_client_settings()
        w = int(str(data.get("image_gen_width") or "").strip())
        h = int(str(data.get("image_gen_height") or "").strip())
    except (TypeError, ValueError):
        return None
    if not (32 <= w <= 4096 and 32 <= h <= 4096):
        return None
    return w, h


def _tier_override() -> str:
    """The user's pinned model line, or 'auto'.

    Priority (2026-10-03): the per-machine pick from the CLIENT Settings UI Runtime tab
    ('image_gen_tier' in <CLIENT>/config/settings.json - read live on every call) wins;
    when that key is absent/invalid it falls back to IMAGE_GEN_TIER_OVERRIDE, delivered
    by the SERVER via config_constants (injected exec global on the CLIENT, imported
    directly in local dev). Anything that is not a known tier name is treated as 'auto'
    with a warning - a typo must never break generation.
    """
    client_tier = _client_image_gen_tier()
    if client_tier:
        return client_tier

    v = globals().get("IMAGE_GEN_TIER_OVERRIDE")  # noqa: B009 - injected symbol by design
    if isinstance(v, str):
        t = v.strip().lower()
        if t in TIER_MODELS:
            return t
        if t and t != "auto":
            logger.warning(f"  IMAGE_GEN_TIER_OVERRIDE={v!r} is not a known tier "
                           f"(expected 'auto'|'high'|'mid'|'low') - using auto classification")
    return "auto"


def _tier_config_for(tier: str) -> dict:
    """The pipeline config for a tier, preferring the SERVER-shipped manifest table.

    A legacy server's TOOL_MANIFEST (v6 and earlier) carries no 'pipeline' field in its
    tiers - in that case the in-file TIER_MODELS is used, so new client behavior never
    depends on a manifest upgrade. Unknown tiers fall back to the safe high config.
    """
    m = globals().get("TOOL_MANIFEST")  # noqa: B009 - injected symbol by design
    if isinstance(m, dict):
        table = m.get("tier_models")
        if isinstance(table, dict) and tier in table and isinstance(table[tier], dict) \
                and table[tier].get("model_id"):
            return dict(table[tier])
    return dict(TIER_MODELS.get(tier) or TIER_MODELS["high"])


def _effective_timeout(cfg: dict) -> int:
    """Subprocess budget for the generation step.

    Resident tiers (mid Z-Image fp8, low SANA-Sprint when not offloaded): 
    IMAGE_SUBPROCESS_TIMEOUT - a warm resident model generates in seconds. Offloaded
    tiers (high Qwen/Qwen-Image on every call, low SANA-Sprint below 12 GB) get a larger
    budget: the first run warms slowly and, on a fresh machine, may still be finishing
    its multi-GB download here when pre-flight was skipped offline. The server constant
    IMAGE_SUBPROCESS_TIMEOUT_OFFLOAD wins when injected; otherwise the in-file fallback
    applies.
    """
    if cfg.get("offload"):
        v = globals().get("IMAGE_SUBPROCESS_TIMEOUT_OFFLOAD")  # noqa: B009
        if isinstance(v, int) and v > 0:
            return v
        return _OFFLOAD_TIMEOUT_FALLBACK
    try:
        t = IMAGE_SUBPROCESS_TIMEOUT  # noqa: F821 - injected on client / imported on server
        if isinstance(t, int) and t > 0:
            return t
    except NameError:
        pass
    return 300


# ---------------------------------------------------------------------------
# Live model-download progress (2026-09-21). _PROGRESS_TQDM_SOURCE is embedded in the
# prep script below, which runs INSIDE THE TOOL VENV. It defines a tqdm-compatible
# class that huggingface_hub's snapshot_download uses for its per-file bars: instead of
# redrawing terminal bars it prints one compact JSON line to stdout every ~0.5 s:
#     {"type": "file", "filename": "...", "size": N, "downloaded": N,
#      "speed_bps": F, "eta_sec": F}
# _ensure_model_cached() streams those lines and merges them into the runtime's
# .setup_progress.json (stage 'model') - the same file the venv/pip stages write.
# The MODEL_READY/MODEL_NOT_READY contract is unchanged: progress lines are extra
# stdout that only feeds the UI; a tracking failure can never break the download.
# ---------------------------------------------------------------------------

_PROGRESS_TQDM_SOURCE = r'''import sys as _sys, json as _json, time as _time


class ProgressTqdm:
        """tqdm-compatible progress bar that PRINTS JSON lines instead of redrawing bars.

        huggingface_hub 1.x (verified against the hf source, 2026-09-21) builds FIVE
        kinds of bars from this class during snapshot_download:

          * per-file download bars - desc is the file name (http path) or '<name>:
            reconstructing file' / '<name>: downloading bytes' (xet path; both route to
            ONE instance because we expose update_transfer). These carry the real file
            names and are what the UI 'Current' line shows.
          * "Fetching N files" counter, "Downloading bytes" network aggregate and
            "Reconstructing ..." on-disk aggregate (snapshot level - no file name).

        Every event a per-file bar emits ALSO carries the running AGGREGATE totals
        (agg_done/agg_size = sum over all open bars + manifest-known sizes) so the UI
        'Total' line is always live even when only one file downloads at a time.
        close() emits one final event; for PER-FILE BARS it carries done=True (that is how a
        finished file row is marked). Count/total/phase close events carry their final numbers
        WITHOUT the flag - and the server-side merge additionally ignores any bool 'done' on a
        count event, so an older running script can never clobber files_done. Speed is the bytes/second average since this bar
        started (stable, no cold-start spikes). Everything is wrapped so a progress
        failure can never break the download; MODEL_READY/MODEL_NOT_READY untouched.

        Event shapes:
          count   {"type":"count","done":N,"total":M}
          total   {"type":"total","downloaded":N,"size":M,"speed_bps":F}
          phase   {"type":"phase","label":...,"downloaded":N,"size":M,"speed_bps":F}
          file    {"type":"file","filename":...,"size":M,"downloaded":N,
                   "speed_bps":F,"eta_sec":F,"agg_done":A,"agg_size":B[, "done":true]}
        """

        _lock = None  # class-level lock shared by hf_thread_map worker threads (tqdm API)
        _bars = {}    # id(self) -> bar: registry so any open bar can compute the aggregate

        @classmethod
        def get_lock(cls):
            if cls._lock is None:
                import threading as _threading
                cls._lock = _threading.Lock()
            return cls._lock

        @classmethod
        def set_lock(cls, lock):
            cls._lock = lock

        # -- bar classification (by description) ------------------------------------
        @staticmethod
        def _classify(desc):
            d = str(desc or "").strip()
            if d.lower().startswith("[dry-run]"):  # snapshot_download's dry-run prefix
                d = d[len("[dry-run]"):].strip()
            for suf in (": reconstructing file", ": downloading bytes"):  # xet direct-mode reporter bars
                if d.lower().endswith(suf):
                    d = d[: -len(suf)].strip()
            low = d.lower()
            if low.startswith("fetching") and "file" in low:
                return "count"
            if low == "downloading bytes":
                return "total"
            if low.startswith("reconstruct") or low in ("download complete", "reconstruction complete"):
                return "phase"
            if "/" in d or "." in d:  # looks like a file name / URL tail (per-file bar)
                return "file"
            return "phase"

        @staticmethod
        def _clean_name(desc):
            """File name for the UI: strip hf's truncation markers and xet suffixes."""
            d = str(desc or "").strip()
            if d.lower().startswith("[dry-run]"):
                d = d[len("[dry-run]"):].strip()
            for suf in (": reconstructing file", ": downloading bytes"):
                if d.lower().endswith(suf):
                    d = d[: -len(suf)].strip()
            d = d.replace("(…)", "").replace("(...)", "")  # hf truncates long names to 40 chars
            return d[:300]

        def __init__(self, desc=None, total=None, unit=None, initial=0, **kwargs):
            self.desc = str(desc or "")[:300]
            self._kind = self._classify(self.desc)
            try:
                self._total = int(total) if total else 0
            except (TypeError, ValueError):
                self._total = 0
            try:
                self._n = max(0, int(initial)) if initial else 0
            except (TypeError, ValueError):
                self._n = 0
            self._t_start = None    # first byte update - anchor for the speed average
            self._last_emit = 0.0   # throttle anchor (~0.5 s)
            self._dirty = False     # n/total changed since the last emit
            self._closed = False
            # Network bytes received for this file (xet 'downloading bytes' side of the
            # bar). hf 1.33 shares ONE bar between transfer and reconstruction and calls
            # update_transfer() continuously while update() (bytes assembled on disk) may
            # not move until the file is finished - see _live.
            self._transfer = 0
            try:
                ProgressTqdm._bars[id(self)] = self
            except Exception:
                pass

        @property
        def total(self):
            return self._total

        @total.setter
        def total(self, value):
            try:
                v = int(value) if value else 0
            except (TypeError, ValueError):
                v = 0
            if v > self._total:     # monotonic - hf only ever grows it
                self._total = v
                self._dirty = True

        @property
        def n(self):
            return self._n

        @n.setter
        def n(self, value):
            try:
                v = int(value) if value else 0
            except (TypeError, ValueError):
                v = 0
            self._n = max(0, v)
            self._dirty = True

        # -- emit --------------------------------------------------------------------
        @property
        def _live(self):
            """Live byte count for THIS file: reconstruction or network, whichever is ahead.

            Verified against huggingface_hub 1.33 + hf_xet (real 253 MB download): the xet
            path calls update_transfer() ~every 0.1 s with network bytes, but calls
            update() only at the very END (67 MB, then 186 MB, then close). Reporting only
            `n` therefore froze the UI for the entire file - minutes of "4.6 GB / 4.6 GB"
            while the network was clearly busy. The transfer counter is the only live
            signal during an xet download, so the row reports max(n, transfer), capped at
            the file size. When the file completes, `n` reaches total and wins anyway.
            """
            n = int(self._n)
            transfer = int(getattr(self, "_transfer", 0) or 0)
            live = n if n >= transfer else transfer
            total = int(self._total or 0)
            return min(live, total) if total > 0 else live

        def _speed_bps(self, now):
            """Bytes/second average since the first update - stable, no burst spikes."""
            if self._t_start is None or now <= self._t_start:
                return 0.0
            return max(0.0, float(self._live)) / max(now - self._t_start, 1e-6)

        @staticmethod
        def _aggregate():
            """Running totals over ALL open per-file bars (done ones included)."""
            done = size = 0
            try:
                for b in list(ProgressTqdm._bars.values()):
                    if getattr(b, "_kind", None) != "file":
                        continue
                    t = int(getattr(b, "_total", 0) or 0)
                    n = int(getattr(b, "_live", 0) or 0)
                    size += t
                    done += n
            except Exception:
                pass
            return done, size

        def _emit(self):
            try:
                now = _time.time()
                payload = {"type": self._kind}
                if self._kind == "count":
                    payload["done"] = self._n
                    payload["total"] = self._total
                elif self._kind == "file":
                    speed = self._speed_bps(now)
                    agg_done, agg_size = self._aggregate()
                    live = self._live
                    payload["filename"] = self._clean_name(self.desc)
                    payload["size"] = self._total
                    payload["downloaded"] = live
                    if speed > 0:
                        payload["speed_bps"] = round(speed, 1)
                        if self._total > live:
                            payload["eta_sec"] = round(min((self._total - live) / max(speed, 1.0), 86400.0), 1)
                    payload["agg_done"] = agg_done
                    payload["agg_size"] = agg_size
                else:  # total / phase aggregates (snapshot level)
                    speed = self._speed_bps(now)
                    payload["downloaded"] = self._n
                    payload["size"] = self._total
                    if self._kind == "phase":
                        payload["label"] = self.desc
                    if speed > 0:
                        payload["speed_bps"] = round(speed, 1)
                # done=True ONLY for per-file bars - on close OR when a file bar reached its own
                # total (xet bars stay open, so the 100% event is their only completion signal).
                # Count/total/phase bars must NEVER carry it: hf closes the 'Fetching N files'
                # counter at run end and int(True) == 1 would clobber files_done to 1. A count
                # close event keeps its real done=N (the number of finished files).
                if self._kind == "file" and (self._closed or (self._total > 0 and self._n >= self._total)):
                    payload["done"] = True
                _sys.stdout.write(_json.dumps(payload) + "\n")
                _sys.stdout.flush()
            except Exception:
                pass

        def _maybe_emit(self, force=False):
            """Throttled emit: at most ~2 JSON lines/second per bar while it is active."""
            now = _time.time()
            if not force:
                if (now - self._last_emit) < 0.5:
                    return
                if not self._dirty:
                    return
            self._last_emit = now
            self._dirty = False
            self._emit()

        def update(self, n=1):
            try:
                now = _time.time()
                added = int(n) if n else 0
                self.n += added    # property setter clamps and marks dirty
                if added > 0 and self._t_start is None:
                    self._t_start = now
                done = (self._total > 0 and self._n >= self._total) or self._closed
                # A finished file must be marked done EVEN IF hf never closes its bar (the xet
                # reporter leaves aggregated bars open - verified in the hf source). Emit once.
                if done:
                    if not self._dirty and not self._closed and getattr(self, "_done_emitted", False):
                        return
                    self._done_emitted = True
                    self._maybe_emit(force=True)
                    return
                self._maybe_emit()
            except Exception:
                pass  # progress must never break the download

        def update_transfer(self, n=1):
            """Network bytes received for this file (the xet path's ONLY live signal).

            huggingface_hub 1.33 shares one bar between the transfer and reconstruction
            reporters: update_transfer() fires every ~0.1 s while the file streams,
            update() (bytes assembled) can stay at 0 until the download is over. Ignoring
            this method - which the previous shim did - meant a 5 GB shard produced NO
            event for its whole download and the UI showed the previous finished file for
            minutes. The counter feeds _live and drives the same throttled emit.
            """
            try:
                added = int(n) if n else 0
                if added > 0:
                    self._transfer = int(getattr(self, "_transfer", 0) or 0) + added
                    if self._t_start is None:
                        self._t_start = _time.time()
                    self._dirty = True
                    self._maybe_emit()
            except Exception:
                pass  # progress must never break the download

        def close(self):
            try:
                if self._closed:
                    return
                self._closed = True
                # A closed file bar means the file is complete: report it at its full size
                # even when the live counters lagged (xet reconstruction may under-report).
                if self._total > 0 and self._n < self._total:
                    self._n = self._total
                self._emit()       # final state (done=True) - the UI marks this file finished
            except Exception:
                pass
            finally:
                try:
                    ProgressTqdm._bars.pop(id(self), None)  # keep the aggregate registry bounded
                except Exception:
                    pass

        # -- tqdm API surface used by huggingface_hub ---------------------------------
        def refresh(self, *args, **kwargs):
            try:
                if not self._closed and self._dirty:
                    self._maybe_emit(force=True)  # hf calls this right after total growth
            except Exception:
                pass

        def set_description(self, desc=None, refresh=True):
            try:
                if desc is not None and str(desc).strip():
                    self.desc = str(desc)[:300]
                    old_kind = self._kind
                    self._kind = self._classify(self.desc)  # 'Reconstruction complete' reclassifies
                    if self._kind != "file" and old_kind == "file":
                        pass  # a file bar was never relabeled in hf; keep its name for the UI
                    else:
                        self._last_emit = _time.time()
                        self._emit()   # completion labels reach the UI live (2-3 calls per run)
            except Exception:
                pass

        def set_description_str(self, desc=None, refresh=True):
            self.set_description(desc)

        # Defensive no-ops for the xet reporter surface (direct-download mode only).
        # NOTE: update_transfer is NOT a no-op - it is the only live signal on the xet
        # path and is defined above with the emit logic.
        def set_postfix_str(self, postfix=None, refresh=False):
            pass

        def set_transfer_postfix_str(self, postfix=None, refresh=False):
            pass

        @property
        def format_dict(self):
            return {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            try:
                self.close()
            except Exception:
                pass
            return False


def _progress_tqdm(full_path=None):
        """Factory huggingface_hub passes as tqdm_class (it calls it once per bar).

        *full_path* is the repo-relative path of the file being downloaded. hf's xet bar
        is described with the BASENAME only, so when the full path is known the bar's
        description is upgraded to it - the UI then names 'vae/config.json' instead of a
        bare 'config.json' that exists in three components of the same repo, and the
        server-side merge matches the manifest row exactly instead of guessing by suffix.
        """
        if not full_path:
            return ProgressTqdm
        full_path = str(full_path)
        base = full_path.rsplit("/", 1)[-1]

        class _NamedProgressTqdm(ProgressTqdm):
            def __init__(self, *args, **kwargs):
                desc = kwargs.get("desc")
                if desc is None and args:
                    desc = args[0]
                d = str(desc or "")
                # Only upgrade a BARE basename: some hf paths already pass the repo path
                # (then replacing would produce 'vae/vae/...').
                if base and base in d and full_path not in d:
                    d = d.replace(base, full_path, 1)
                    if args:
                        args = (d,) + tuple(args[1:])
                    else:
                        kwargs["desc"] = d
                super().__init__(*args, **kwargs)

        return _NamedProgressTqdm


'''

# ---------------------------------------------------------------------------

# PER-FILE PROGRESS PATCH (v13, 2026-09-21) - why this exists:

# huggingface_hub 1.x snapshot_download does NOT pass the caller's tqdm_class to the
# per-file downloads: _inner_hf_hub_download() hardcodes its private _AggregatedTqdm
# fake (verified in v1.32.0 source; the docstring says 'the tqdm_class is not passed
# to each individual download'). Without this patch our ProgressTqdm only ever sees
# the three snapshot-level aggregate bars, so per-file rows in .setup_progress.json
# would stay at 0 bytes while the totals move - exactly the frozen 'Current' line the
# UI showed. The patch re-points hf_hub_download's tqdm_class kwarg to our factory for
# the duration of the pre-flight script, so BOTH download paths emit real per-file
# bars: http_get (desc = file name) and xet_get ('<name>: reconstructing file'). It is
# a thin wrapper that touches NOTHING else - args, return value and exceptions pass
# through untouched; it is idempotent (_pf_wrapped flag), version-tolerant (any failure
# leaves hf unpatched and the aggregate-only fallback keeps working) and runs only in
# this short-lived pre-flight process. Informational only: a patching failure can never
# break the download.

_PER_FILE_PATCH_SOURCE = r"""import huggingface_hub as _hf
try:
    import huggingface_hub._snapshot_download as _sdmod
except Exception:
    _sdmod = None


def _pf_install():
    global _sdmod
    if _sdmod is None or getattr(_sdmod.hf_hub_download, '_pf_wrapped', False):
        return  # module missing (very old hf) or already patched - idempotent
    orig = _sdmod.hf_hub_download


    def wrapper(*args, **kwargs):
        try:
            # hf's xet bar is described with incomplete_path.name = the BASENAME only
            # ('config.json'), which is ambiguous across text_encoder/ transformer/ vae/
            # and can be matched to the wrong manifest row. The full repo path is right
            # here in the call - hand it to the bar so the UI shows (and the merge
            # matches) 'vae/config.json'.
            filename = kwargs.get('filename')
            if filename is None and len(args) > 1:
                filename = args[1]
            kwargs['tqdm_class'] = _progress_tqdm(filename)
        except Exception:
            pass  # progress is informational only - never break the download
        return orig(*args, **kwargs)

    wrapper._pf_wrapped = True
    _sdmod.hf_hub_download = wrapper


_pf_install()
"""


def _model_prep_script(model_ids=None) -> str:

        """Python payload run INSIDE THE TOOL VENV to make sure the model(s) are cached.

        Online: snapshot_download verifies/completes each HF cache (resumable - a killed
        download continues where it left off). Offline or HF unreachable: falls back to a
        local-only check so an ALREADY-COMPLETE cache keeps working without network; only
        'incomplete cache + no network' fails that model. Prints one line per model and
        exits 2 when ANY requested model is unavailable (the caller treats this as a
        warning - the worker has its own download budget, see _ensure_model_cached).

        LIVE PROGRESS (2026-09-21): before each snapshot_download the script pre-fetches
        the repo file list + sizes (HfApi.list_repo_tree) and emits one 'manifest' event -
        that gives the UI the true total from second 0. Then snapshot_download gets
        tqdm_class=_progress_tqdm() so its aggregate bars emit throttled JSON progress
        lines; v13 additionally installs _PER_FILE_PATCH_SOURCE first, because hf 1.x
        hardcodes a private fake class for per-file downloads and would otherwise never
        hand our factory to them. With the patch BOTH download paths (http_get: desc =
        file name; xet_get: '<name>: reconstructing file') emit REAL per-file bars with
        live bytes - that is what feeds the UI 'Current' line. The caller streams stdout
        and merges everything into .setup_progress.json. A TypeError fallback keeps older
        huggingface_hub versions working unchanged.

        BUG FIX v12: the failure path used bare 'sys.exit(2)' but this -c script only
        imports 'sys as _sys' (via the embedded class source) - NameError on every failed
        pre-flight. Now '_sys.exit(2)' so MODEL_NOT_READY + exit 2 actually happen.
        """

        ids = [str(m) for m in (model_ids or [_MODEL_ID]) if str(m)]

        return (

            _PROGRESS_TQDM_SOURCE +

            # v13: re-point hf's per-file download bars at our factory BEFORE any
            # snapshot_download runs (hf 1.x hardcodes its private fake class there).
            _PER_FILE_PATCH_SOURCE +

            "models = " + repr(ids) + "\n"

            # The FP8 repo ships an alternate e5m2 transformer nobody loads - skip it so a

            # client that still uses the v6 table downloads ~14 GB instead of ~20 GB.

            "ignore = {'ykarout/Z-Image-Turbo-FP8-Full': ['transformer/diffusion_pytorch_model_e5m2.safetensors']}\n"

            # Manifest pre-fetch (v12): the true file list + sizes BEFORE the download starts,

            # so the UI 'Total' line has a real denominator from second 0. One cheap API call

            # per repo; any failure is ignored (progress simply falls back to bar aggregates).

            "import fnmatch as _fnmatch\n"

            "_manifests = {}\n"

            "def _emit_manifest(mid, pats):\n"

            "    try:\n"

            "        from huggingface_hub import HfApi\n"

            "        files = []\n"

            "        for e in HfApi().list_repo_tree(mid, recursive=True):\n"

            "            if not hasattr(e, 'size'):\n"

            "                continue  # folders carry no size - only RepoFile entries count\n"

            "            p = str(getattr(e, 'path', '') or '')\n"

            "            if any(_fnmatch.fnmatch(p, pat) for pat in (pats or [])):\n"

            "                continue  # mirror snapshot_download's ignore_patterns exactly\n"

            "            files.append({'filename': p, 'size': int(e.size)})\n"

            "        _manifests[mid] = files\n"

            "    except Exception:\n"

            "        pass\n"

            "# v13: per-file progress - route hf's per-file download bars to our factory.\n"

            "try:\n"

            "    _pf_install()\n"

            "except Exception:\n"

            "    pass  # aggregate-only fallback keeps working; never break the download\n"

            "failed = []\n"

            "try:\n"

            "    from huggingface_hub import snapshot_download\n"

            "except Exception as exc:\n"

            "    print('MODEL_NOT_READY: ' + str(exc)[:400], flush=True)\n"

            "    _sys.exit(2)\n"

            "for i, model_id in enumerate(models):\n"

            "    try:\n"

            "        ig = ignore.get(model_id)\n"

            "        _emit_manifest(model_id, ig)\n"

            "        mfiles = _manifests.get(model_id) or []\n"

            "        if mfiles:\n"

            "            print(_json.dumps({'type': 'manifest', 'repo_index': i + 1, 'repo_count': len(models), 'files': mfiles}), flush=True)\n"

            "        try:\n"

            # SEQUENTIAL DOWNLOAD (2026-10-05): max_workers=1 - exactly ONE file in flight at a time.
            # The UI shows one stable per-file row with live bytes/speed/ETA from the first
            # download to the last; parallel bars interleaved and re-opened rows, which is
            # what produced the "file in progress -> same file again from zero" display.
            # Resume is unaffected (HF .incomplete + Range still apply).

            "            path = snapshot_download(model_id, ignore_patterns=ig, max_workers=1, tqdm_class=_progress_tqdm())\n"

            "        except TypeError:\n"

            # Older huggingface_hub may reject one of the kwargs - degrade gracefully.

            "            try:\n"

            "                path = snapshot_download(model_id, ignore_patterns=ig, max_workers=1)\n"

            "            except TypeError:\n"

            "                path = snapshot_download(model_id, ignore_patterns=ig)\n"

            "        except Exception:\n"

            "            # Offline or HF unreachable: trust a complete local cache.\n"

            "            path = snapshot_download(model_id, local_files_only=True, ignore_patterns=ig)\n"

            "        print('MODEL_READY: ' + model_id + ': ' + str(path), flush=True)\n"

            "    except Exception as exc:\n"

            "        failed.append(str(exc)[:300])\n"

            "if failed:\n"

            "    print('MODEL_NOT_READY: ' + '; '.join(failed), flush=True)\n"

            "    _sys.exit(2)\n"

        )


# ---------------------------------------------------------------------------
# Tool-local model cache (2026-09-12): EVERY file this tool needs lives inside its
# own runtime folder - <runtime_dir>/hf_cache holds the Hugging Face weights, next
# to venv/ and the worker script. One folder = see it / delete it. The same
# convention applies to every future self-unpacking tool (see tool_bootstrap.py).
# ---------------------------------------------------------------------------

def _tool_env(rt: dict) -> dict:
    """Environment for this tool's subprocesses: HF_HOME inside the runtime dir.

    huggingface_hub honors HF_HOME, so snapshot_download / from_pretrained store and
    read all model files under <runtime_dir>/hf_cache instead of the user's default
    %USERPROFILE%\\.cache\\huggingface. The venv is unaffected (its interpreter path
    was created by the bootstrap independently of any cache location).
    """
    env = dict(os.environ)
    env["HF_HOME"] = os.path.join(rt["runtime_dir"], "hf_cache")
    return env


def _migrate_legacy_model_cache(rt: dict) -> None:
    """One-time move of a pre-upgrade model cache from the default user HF location.

    Machines that ran an older build already hold Z-Image weights in
    %USERPROFILE%\\.cache\\huggingface\\hub (the old default). Instead of making them
    re-download, move it once into <runtime_dir>/hf_cache. Never touches any OTHER
    model in the user cache - only this tool's own folders are moved. Skipped entirely
    when the new home already holds ANY known repo (a mid/low-tier machine may
    legitimately have ONLY SANA-Sprint or the FP8 variant there). A failed move
    degrades to a fresh download (resumable) and never blocks generation.
    """
    runtime_dir = rt["runtime_dir"]
    hub_new = os.path.join(runtime_dir, "hf_cache", "hub")
    if os.path.isdir(hub_new):
        for model_id in KNOWN_MODEL_IDS:
            if os.path.exists(os.path.join(hub_new, f"models--{model_id.replace('/', '--')}")):
                return  # a known repo already lives at the new home

    legacy_hub = os.path.normpath(os.path.join(
        os.environ.get("HF_HOME") or os.path.expanduser("~"), ".cache", "huggingface", "hub"))
    for model_id in KNOWN_MODEL_IDS:
        legacy_model_dir = os.path.join(legacy_hub, f"models--{model_id.replace('/', '--')}")
        if not os.path.isdir(legacy_model_dir):
            continue  # nothing to migrate for this repo

        new_model_dir = os.path.join(hub_new, f"models--{model_id.replace('/', '--')}")
        logger.info(f"  Migrating legacy model cache {legacy_model_dir} -> {new_model_dir}")
        try:
            os.makedirs(hub_new, exist_ok=True)
            shutil.move(legacy_model_dir, new_model_dir)
        except Exception as e:
            # Cross-drive or partial-move issues: clean up any half-moved folder so the
            # next call retries from a clean slate; generation falls back to downloading.
            if os.path.isdir(new_model_dir):
                shutil.rmtree(new_model_dir, ignore_errors=True)
            logger.warning(f"  Legacy model cache migration failed ({e}) - "
                           f"a fresh download will be used instead")


def _tb_symbol(name):
    """Resolve a public symbol of tools.tool_bootstrap from this tool's context.

    CLIENT: the loader injects every shared-module public symbol into the tool's exec
    globals, so the bare name is found via globals(). LOCAL DEV: no injection happens -
    fall back to sys.modules (already imported by an earlier tool) or a real package
    import of tools.tool_bootstrap. The function-call form below keeps this invisible
    to the client source transform, which only rewrites import STATEMENTS. Progress is
    INFORMATIONAL ONLY - any failure degrades to 'no progress shown', never breaks setup.
    """
    try:
        val = globals().get(name)  # noqa: B009 - injected shared symbol on the CLIENT
        if val is not None and name != "globals":
            return val
        mod = sys.modules.get("tools.tool_bootstrap")
        if mod is not None:
            return getattr(mod, name, None)
        import importlib  # local dev only (client transform leaves this untouched)
        mod = importlib.import_module("tools.tool_bootstrap")
        return getattr(mod, name, None)
    except Exception as e:
        logger.debug(f"  tool_bootstrap symbol '{name}' unavailable ({e})")
        return None


def _setup_progress(rt: dict):
    """The shared SetupProgress writer for this runtime (None if unavailable).

    Resolves the class through _tb_symbol() so progress works both on the CLIENT
    (injected exec globals) and in local dev (real package import). Progress is
    INFORMATIONAL ONLY - any failure degrades to 'no progress shown', never breaks setup.
    """
    try:
        sp_cls = _tb_symbol("SetupProgress")
        if sp_cls is None:
            logger.debug("  setup progress unavailable - continuing without it")
            return None
        return sp_cls(rt["runtime_dir"])
    except Exception as e:
        logger.debug(f"  setup progress unavailable ({e}) - continuing without it")
        return None


def _norm_bar_name(name: str):
    """Normalize a per-file bar name for matching (2026-10-05 audit fix).

    The prep script strips the xet suffixes (': reconstructing file' / ': downloading
    bytes') and the truncation markers, but variants survive: a name that is EXACTLY
    the file path plus a bare colon (suffix cut at the 300-char cap) or the full suffix.
    Such names match no manifest row and would spawn a phantom twin row. Cut everything
    from the first ': ' onward, then strip trailing colons - the file path part always
    precedes any xet suffix (HF repo paths never contain ':').
    """
    n = str(name or "").strip()
    cut = n.find(": ")
    if cut > 0:
        n = n[:cut]
    while n.endswith(":"):
        n = n[:-1].rstrip()
    return n


def _match_known_file(model_state: dict, name: str, size=None):
    """Match a per-file bar's (possibly truncated) name to its manifest row.

    hf truncates long names for display - http_get keeps the LAST 40 chars with a
    leading '(...)', xet_get keeps the FIRST 40 + trailing '(...)'. The prep script
    already strips those markers, so here we match by exact path first, then by
    suffix (handles both truncation styles), then by prefix. Matching is scoped to
    the CURRENT repo's rows (2026-10-05 sequential fix) - a file name that exists in
    an earlier repo must not be re-used for this repo's bar.

    A bar may also carry only the BASENAME (hf's xet path, and any name long enough to be
    truncated): several components of a repo ship 'config.json', so an ambiguous match is
    resolved by the event's own size when exactly one candidate has it. If that still
    leaves several candidates we deliberately report 'unknown' rather than misattribute a
    multi-GB shard to the wrong row.

    Returns (canonical_name, exact_match): exact_match=False means no manifest row
    matched and canonical_name is the raw given name - the caller must then treat it
    as UNKNOWN and never create a duplicate row blindly (see _merge_model_progress).
    """
    files = model_state.get("files") or {}
    if not files:
        return name, True
    n = _norm_bar_name(name)
    if not n:
        return name, True
    cur_repo = model_state.get("current_repo")
    try:
        want_size = int(size) if size is not None else 0
    except (TypeError, ValueError):
        want_size = 0

    def _scoped():
        for key in files:
            v = files[key]
            if cur_repo is not None and v.get("_repo") not in (None, cur_repo):
                continue  # row belongs to an earlier repo - out of scope
            yield key, v

    def _path(key):
        # Row keys are "<repo>:<path>" (per-repo scoping); match on the bare path part.
        return key.split(":", 1)[1] if ":" in key else key

    def _narrow(hits):
        """One candidate, or the single candidate whose manifest size matches the event."""
        if len(hits) == 1:
            return hits[0]
        if want_size > 0:
            by_size = [k for k in hits if int(files[k].get("size") or 0) == want_size]
            if len(by_size) == 1:
                return by_size[0]
        return None

    # 1) exact (manifest rows' bare paths)
    for key, v in _scoped():
        if _path(key) == n and "size" in v:
            return key, True
    # 2) suffix match - a truncated tail is always a suffix of the full path
    hits = [k for k, v in _scoped() if len(n) >= 4 and _path(k).endswith(n)]
    pick = _narrow(hits)
    if pick:
        return pick, False
    # 3) prefix match (xet keeps the FIRST 40 chars + marker)
    hits = [k for k, v in _scoped() if len(n) >= 12 and _path(k).startswith(n[:40])]
    pick = _narrow(hits)
    if pick:
        return pick, False
    # 4) same basename as an existing current-repo row - a display variant of a file we already track
    base = n.rsplit("/", 1)[-1]
    if len(base) >= 4:
        pick = _narrow([k for k, v in _scoped() if _path(k).rsplit("/", 1)[-1] == base])
        if pick:
            return pick, False
    return n, False


def _pick_current_file(model_state: dict):
    """The file to show on the UI Current line (v14/v15 + repo-scoped, 2026-10-05).

    Only rows that received a REAL per-file bar event (_seen=True) are eligible -
    manifest-only rows sit at 0 bytes and must never be named as 'current'. Rows of
    the CURRENT repo win over earlier repos' leftovers (between-repo gap: the previous
    repo's just-finished row may still be shown, which is honest). Pass 1: among seen
    + unfinished rows the most recently updated one wins (global _seq recency marker;
    ties break toward more bytes downloaded). Pass 2: when nothing is in flight but a
    seen row already holds real bytes, that most recently updated row is returned so
    the UI Current line keeps showing a REAL file with its real numbers. Returns None
    only while zero bytes are on the ground - then the UI shows a plain
    'Downloading model files' line.
    """
    files = model_state.get("files") or {}
    cur_repo = model_state.get("current_repo")

    def _repo_rank(v):
        return 1 if (cur_repo is None or v.get("_repo") in (None, cur_repo)) else 0

    best, best_key = None, (-1, -1, -1)
    for k in files:
        v = files[k]
        if not v.get("_seen"):
            continue  # manifest-only row - never 'current' (v14 ghost-row fix)
        if v.get("done"):
            continue
        got = int(v.get("downloaded") or 0)
        sz = int(v.get("size") or 0)
        if sz and got >= sz:
            continue  # effectively finished even without the explicit flag
        key = (_repo_rank(v), int(v.get("_upd") or 0), 1 if got > 0 else 0, got)
        if key > best_key:
            best, best_key = k, key
    if best is not None:
        return best
    # Pass 2 - nothing in flight right now (between files / finalize tail): name the most
    # recently updated row that has REAL bytes. The UI Current line must always show a real
    # file with its real numbers, never an aggregate label. Manifest-only rows (0 bytes)
    # are still excluded - they have nothing to show.
    best, best_key = None, (-1, -1, -1)
    for k in files:
        v = files[k]
        if not v.get('_seen'):
            continue
        got = int(v.get("downloaded") or 0)
        sz = int(v.get("size") or 0)
        if not (sz and got > 0):
            continue
        key = (_repo_rank(v), int(v.get("_upd") or 0), got)
        if key > best_key:
            best, best_key = k, key
    return best


def _sweep_ghost_rows(model_state: dict, final: bool = False) -> None:
    """Mark manifest-only rows done once NO bytes can be in flight anymore (v14).

    hf 1.x returns early without creating any progress bar for files that are already
    cached or land too fast to ever open a bar. Those rows would stay at 0 bytes /
    done=false forever and pin the UI Current line on a fake 'starting' file.

    Triggers are AUTHORITATIVE completion signals only - never byte arithmetic:
      * final=True - the prep script printed MODEL_READY for this repo's run:
        snapshot_download returned, so anything left over is definitively on disk;
      * the 'Fetching N files' counter reports all files finished (files_done ==
        files_total) - hf's own completion signal.

    2026-10-05 sequential fix: a per-repo counter completion must ONLY sweep the
    CURRENT repo's rows - earlier repos' manifest-only rows are already settled and
    their done flags / bytes must not be touched again (that is what made finished
    files look like they restarted from zero when the next repo began).

    Mid-run (counter completion) only rows that never received a real per-file event
    (_seen unset) are swept; a row with live progress is left to its own close event.
    final=True is the end of the WHOLE run (MODEL_READY for every repo), so every row
    still marked unfinished is completed at full size - including seen rows whose bar
    never reported its close (2026-10-23: those left the Total line below 100%).
    Informational only: this changes what the state file shows, never the download.
    """
    files = model_state.get("files") or {}
    if not files:
        return
    try:
        f_total = int(model_state.get("files_total") or 0)
        f_done = int(model_state.get("files_done") or 0)
    except (TypeError, ValueError):
        return
    counter_done = f_total > 0 and f_done >= f_total
    if not (final or counter_done):
        return
    cur_repo = model_state.get("current_repo")
    for v in files.values():
        if v.get("done"):
            continue
        if not final:
            # Mid-run counter completion: only rows that never opened a bar can be swept
            # (a seen row may still be finishing its own bar); earlier repos are settled.
            if v.get("_seen"):
                continue
            if cur_repo is not None and v.get("_repo") not in (None, cur_repo):
                continue  # earlier repo's row - already settled by its own sweep
        # final=True runs only after the prep script printed MODEL_READY for EVERY repo -
        # snapshot_download returned, so whatever row is left IS on disk. That includes
        # seen rows whose bar never reported its close (2026-10-23: trusting only the
        # explicit flag left such rows below 100% forever).
        sz = int(v.get("size") or 0)
        v["done"] = True
        if sz > 0 and int(v.get("downloaded") or 0) < sz:
            v["downloaded"] = sz  # it was on disk - show the row complete, not 0%


def _parse_model_progress_line(line: str):
    """Parse one prep-script stdout line; returns (event_dict | None, is_contract_line).

    Contract lines ('MODEL_READY'/'MODEL_NOT_READY') are detected separately so the
    existing ready-check logic keeps working byte-for-byte. JSON progress events from
    the tqdm class arrive as {'type': 'count'|'total'|'phase'|'file', ...}; the prep
    script also emits one {'type': 'manifest', 'files': [...]} line per repo (v12).

    Anything else is ignored. Informational only - a parse failure never affects the
    download.
    """
    s = (line or "").strip()
    if not s:
        return None, False
    if s.startswith("MODEL_READY:") or s.startswith("MODEL_NOT_READY:"):
        return None, True
    try:
        ev = json.loads(s)
    except Exception:
        return None, False
    if isinstance(ev, dict) and ev.get("type") in ("count", "total", "phase", "file", "manifest"):

        return ev, False

    return None, False


def _repo_rows(model_state: dict):
    """File rows belonging to the CURRENT repo (rows without a tag count as current)."""
    files = model_state.get("files") or {}
    cur = model_state.get("current_repo")
    if cur is None:
        return list(files.values())
    return [v for v in files.values() if v.get("_repo") in (None, cur)]


def _recompute_totals(model_state: dict) -> None:
    """Rebuild the stage totals from file rows (in place).

    With max_workers=1 the per-file rows are complete and monotonic - they ARE the
    statistics. The LIVE numbers (total_size / downloaded / files_done) describe the repo
    being fetched right now, so a multi-repo run shows each repo's own 0->100% arc; the
    grand totals across ALL repos are kept separately as *_grand for context."""
    files = model_state.get("files") or {}
    if not files:
        return
    vals = _repo_rows(model_state)
    cur_total = sum(int(v.get("size") or 0) for v in vals)
    cur_bytes = sum(int(v.get("downloaded") or 0) for v in vals)
    cur_done = sum(1 for v in vals if v.get("done"))

    all_total = sum(int(v.get("size") or 0) for v in files.values())
    all_bytes = sum(int(v.get("downloaded") or 0) for v in files.values())
    all_done = sum(1 for v in files.values() if v.get("done"))

    # live (current repo) - monotonic within the run: re-emitted events must never push
    # numbers backwards. A NEW repo's manifest resets its own arc to 0 (see manifest branch).
    if cur_total > int(model_state.get("total_size") or 0):
        model_state["total_size"] = cur_total
    if cur_bytes > int(model_state.get("downloaded") or 0):
        model_state["downloaded"] = cur_bytes
    if cur_done > int(model_state.get("files_done") or 0):
        model_state["files_done"] = cur_done

    # grand totals across all repos (context for the UI's 'Repo X of Y' line)
    if all_total > int(model_state.get("total_size_grand") or 0):
        model_state["total_size_grand"] = all_total
    if all_bytes > int(model_state.get("downloaded_grand") or 0):
        model_state["downloaded_grand"] = all_bytes
    if all_done > int(model_state.get("files_done_grand") or 0):
        model_state["files_done_grand"] = all_done


def _update_stage_speed(model_state: dict, ev_speed=None) -> None:
    """Live stage speed/ETA for the Total line, derived from this repo's byte arc.

    The timing anchor is set by the repo's manifest (or lazily by its first event), so
    the rate is 'bytes moved since this repo started / wall time since then'. That is
    stable under max_workers=1 and - unlike the old aggregate-bar average - still correct
    when huggingface_hub emits only per-file events (2026-10-23: a file-only stream left
    the Total line permanently without speed/ETA). Falls back to the event's own average
    for the first ~1.5 s, before the anchor has enough movement to be meaningful.
    """
    try:
        cur_repo = model_state.get("current_repo")
        if "_repo_t0" not in model_state or model_state.get("_t0_repo") != cur_repo:
            model_state["_repo_t0"] = _time.time()
            model_state["_t0_bytes"] = int(model_state.get("downloaded") or 0)
            model_state["_t0_repo"] = cur_repo
        t0 = float(model_state.get("_repo_t0") or 0)
        elapsed = _time.time() - t0 if t0 else 0.0
        moved = int(model_state.get("downloaded") or 0) - int(model_state.get("_t0_bytes") or 0)
        speed = (moved / elapsed) if (elapsed > 1.5 and moved > 0) else \
            (float(ev_speed) if isinstance(ev_speed, (int, float)) and ev_speed > 0 else 0.0)
        if speed <= 0:
            return
        model_state["speed_bps"] = round(speed, 1)
        total = int(model_state.get("total_size") or 0)
        done = int(model_state.get("downloaded") or 0)
        if total > done:
            model_state["eta_sec"] = round(min((total - done) / speed, 86400.0), 1)
        else:
            model_state.pop("eta_sec", None)
    except Exception as e:  # informational only - never break the download
        logger.debug(f"  stage speed update failed (ignored): {e}")


def _merge_model_progress(model_state: dict, ev: dict) -> None:
    """Merge one progress event into the 'model' stage dict (in place).

    Event types (emitted by ProgressTqdm in the tool venv - see _PROGRESS_TQDM_SOURCE):
      manifest - repo file list + sizes (prep script pre-fetch, v12) -> files{} rows with
                 known size from second 0; carries repo_index/repo_count (sequential rework
                 2026-10-05) so the UI can show 'Repo X of Y'.
      count    - "Fetching N files" counter        -> files_total / files_done (per repo)
      total    - "Downloading bytes" aggregate     -> downloaded floor, speed fallback
      phase    - "Reconstructing ..." on-disk bar  -> downloaded floor, label, speed/ETA
      file     - per-file download bar (REAL name; http desc or xet '<name>: ...')
                 -> files[<canonical>] row with its own bytes/speed/ETA.

    SEQUENTIAL REWORK (2026-10-05) - what changed and why:
      * downloads now run max_workers=1, so exactly ONE file is in flight at a time; the
        per-file rows are the source of truth for total_size / downloaded / files_done,
        giving continuous statistics from the first download to the last.
      * repo awareness: current_repo/repo_index/repo_count track which repo is being
        fetched; earlier repos' rows are never re-touched, so finished files cannot look
        like they restarted at zero when the next repo begins (a new repo's manifest
        starts its own fresh arc instead of wiping the shared state).
      * duplicate-row guard: a per-file bar whose name does NOT match any manifest row
        updates an existing same-basename current-repo row if there is exactly one; it only
        creates a brand-new row when nothing matches - no more phantom 0-byte rows for a
        file that is already in flight.
      * stage speed/ETA: bytes moved since this repo's manifest / elapsed wall time -
        stable under sequential download and correct even when only per-file events
        arrive (the aggregate-bar average was bursty and sometimes absent entirely).

    The stage dict ends up with: status, phase ('downloading'|'finalizing'), total_size,
    downloaded, speed_bps/eta_sec (Total line), current_file? (the ACTIVE row for the
    Current line), files_total, files_done, repo_index/repo_count and files{} - each file
    row has filename/size/downloaded/speed_bps? eta_sec?/done? plus internal _upd/_repo.
    """
    kind = ev.get("type")

    if kind == "manifest":
        # Repo context first: a new manifest = this repo is about to be fetched.
        try:
            idx = int(ev.get("repo_index") or 1)
        except (TypeError, ValueError):
            idx = model_state.get("current_repo")
        if isinstance(ev.get("repo_count"), int) and ev["repo_count"] > 0:
            model_state["repo_count"] = int(ev["repo_count"])

        rows = []
        try:
            for r in (ev.get("files") or []):
                fn = str(r.get("filename") or "").strip()
                sz = int(r.get("size") or 0)
                if fn and sz > 0:
                    rows.append((fn, sz))
        except Exception:
            rows = []

        files = model_state.setdefault("files", {})
        for fn, sz in rows:
            # Row keys are PER-REPO (2026-10-05 audit fix): two repos can ship the SAME
            # relative path (e.g. Qwen base + Edit both carry 'transformer/diffusion_...').
            # A bare-path key would merge repo 2's row into repo 1's - the first repo's done
            # flag/bytes would belong to the wrong file and repo 2's arc would start at a
            # phantom 100%. '<repo>:<path>' keeps every row distinct while the UI-facing
            # 'filename' field stays the bare path.
            key = (f"{idx}:" + fn) if idx is not None else fn
            f = files.setdefault(key, {"filename": fn})
            # One-time migration: pre-per-repo-keys state used the BARE path as key.
            # Fold that legacy row's real progress into this repo-scoped row and drop
            # it - an untagged half-done ghost would pin the UI Current line forever.
            if fn != key and fn in files:
                leg = files[fn]
                for fk in ("downloaded", "size"):
                    fv = leg.get(fk)
                    if isinstance(fv, (int, float)) and fv > int(f.get(fk) or 0):
                        f[fk] = int(fv)
                for fk in ("done", "_seen"):
                    if leg.get(fk):
                        f[fk] = True
                del files[fn]
            # tag the row with its repo - existing rows keep their original tag so a
            # re-emitted manifest never re-homes old rows.
            if "_repo" not in f and idx is not None:
                f["_repo"] = idx
            if sz > int(f.get("size") or 0):
                f["size"] = sz          # manifest size is authoritative; never shrink

        if idx != model_state.get("_arc_repo"):
            # A NEW repo's arc starts here: the live numbers describe THIS repo from now on.
            # Earlier repos' rows keep their done flags and bytes - nothing resets to zero.
            model_state["_arc_repo"] = idx
            model_state["current_repo"] = idx
            # UI-facing alias: the Total line shows "Repo X of Y" from these two keys.
            model_state["repo_index"] = idx
            vals = [v for v in files.values() if v.get("_repo") in (None, idx)]
            model_state["total_size"] = sum(int(v.get("size") or 0) for v in vals)
            # resume: bytes already on disk from a previous attempt are re-claimed here
            model_state["downloaded"] = sum(int(v.get("downloaded") or 0) for v in vals)
            # PER-REPO done count (2026-10-23 audit re-check): files_total is reset to 0 above
            # and refilled by THIS repo's own 'Fetching N files' counter, so files_done must be
            # this repo's arc too - a cumulative count would (a) show 'Files: X of Y done' with
            # the wrong numerator on the UI and (b) trip _sweep_ghost_rows' f_done >= f_total
            # trigger instantly, marking every not-yet-downloaded manifest row 100%.
            model_state["files_done"] = sum(1 for v in vals if v.get("done"))
            # per-repo counter arc: reset so the previous repo's N/M cannot leak into
            # this repo's 'Files done' line until its own count event arrives.
            model_state["files_total"] = 0
            model_state.pop("speed_bps", None)
            model_state.pop("eta_sec", None)
            # speed/ETA anchor for THIS repo: wall clock + bytes already on disk (resume
            # counts as the starting floor, so the rate is not inflated by old bytes).
            model_state["_repo_t0"] = _time.time()
            model_state["_t0_bytes"] = int(model_state["downloaded"] or 0)
            model_state["_t0_repo"] = idx
        else:
            # same repo (re-emitted manifest / retry of the same run) - monotonic only
            _recompute_totals(model_state)
        return

    if kind == "count":
        # v15: a bool 'done' is NOT the file counter - older running scripts emitted done=True
        # on the count bar's close event and int(True) == 1 clobbered files_done to 1 at run end.
        _cd = ev.get("done")
        if isinstance(_cd, bool):
            _cd = None
        try:
            ft = int(ev.get("total") or 0)
            # per-repo counter - only adopt it for the current repo's rows. files_done is
            # monotonic within a run so an early/stale close event can never push it back.
            if model_state.get("files_total") != ft:
                model_state["files_total"] = ft
            if _cd is not None:
                cd = min(int(_cd), int(model_state.get("files_total") or int(_cd)))
                if cd > int(model_state.get("files_done") or 0):
                    model_state["files_done"] = cd
        except (TypeError, ValueError):
            pass

    downloaded = ev.get("downloaded")
    if not isinstance(downloaded, (int, float)) or downloaded < 0:
        downloaded = None
    size = ev.get("size")
    if not isinstance(size, (int, float)) or size <= 0:
        size = None

    if kind == "file":
        raw_name = str(ev.get("filename") or "file")[:300]
        fname, _exact = _match_known_file(model_state, raw_name, size)
        files = model_state.setdefault("files", {})
        f = files.get(fname)
        if f is None:
            # Unknown name (no manifest row matched): create a NEW row only when nothing
            # with the same basename exists in this repo - otherwise we would double-list an
            # already-in-flight file at 0 bytes ("same file again from zero").
            base = _norm_bar_name(raw_name).rsplit("/", 1)[-1]  # path part, suffix cut
            alt = None
            if len(base) >= 4:  # repo-scoped rows make short basenames safe to merge on
                hits = []
                for k, v in files.items():
                    if k == fname or v.get("_repo") not in (None, model_state.get("current_repo")):
                        continue
                    kb = (k.split(":", 1)[1] if ":" in k else k).rsplit("/", 1)[-1]
                    # exact basename, or a display variant that CARRIES it on a word
                    # boundary - e.g. the xet suffix cut mid-word at the 300-char cap
                    # ("...model.safetensors: reconstructing" -> base ends with kb).
                    if kb == base or (len(kb) >= 4 and base.endswith(kb)
                                      and not base[-len(kb)-1].isalnum()):
                        hits.append(k)
                if len(hits) == 1:
                    alt = hits[0]
            f = files.setdefault(alt or fname, {"filename": alt or fname})
        if "_repo" not in f and model_state.get("current_repo") is not None:
            f["_repo"] = model_state["current_repo"]

        if size:
            f["size"] = int(max(int(f.get("size") or 0), int(size)))
        if downloaded is not None:
            cap = f.get("size") or downloaded
            # monotonic per row - a re-opened bar (resume) must never push bytes backwards
            new_val = max(0, min(downloaded, cap))
            f["downloaded"] = int(max(int(f.get("downloaded") or 0), new_val))
        speed = ev.get("speed_bps")
        if isinstance(speed, (int, float)) and speed >= 0:
            f["speed_bps"] = round(float(speed), 1)
        eta = ev.get("eta_sec")
        if isinstance(eta, (int, float)):
            f["eta_sec"] = round(max(0.0, float(eta)), 1)

        # v14: this row received a REAL per-file bar event - it is a live download row.
        f["_seen"] = True

        # recency marker for the Current line (global sequence shared by all file rows).
        seq = int(model_state.get("_seq") or 0) + 1
        model_state["_seq"] = seq
        f["_upd"] = seq

        # done flag: ONLY the explicit completion signal counts (close, or the bar's own
        # byte count reaching the total). 2026-10-23: byte arithmetic on downloaded >= size
        # must not complete a row - xet transfer bytes are capped at the file size, so a
        # still-downloading file could otherwise be marked done at "100%" and the Current
        # line would jump to the next file while this one was still on the wire.
        if ev.get("done"):
            f["done"] = True
            # A finished file is exactly its manifest size, whatever the live counters said
            # (xet transfer bytes are compressed/deduplicated and can land below it).
            if f.get("size"):
                f["downloaded"] = max(int(f.get("downloaded") or 0), int(f["size"]))

    # total / phase aggregates - feed stage.downloaded as a floor (rows are the truth).
    if downloaded is not None:
        cur = int(model_state.get("downloaded") or 0)
        if downloaded > cur:
            model_state["downloaded"] = int(downloaded)

    if kind == "phase":
        label = str(ev.get("label") or "")[:200]
        low = label.lower()
        if "complete" in low:
            model_state["phase"] = "finalizing"
        elif "reconstruct" in low:
            model_state["phase"] = "finalizing"
        else:
            model_state["phase"] = model_state.get("phase") or "downloading"

    # Stage-level speed/ETA (Total line) is computed AFTER the totals are summed from the
    # rows - see _update_stage_speed below. It is row-driven, not aggregate-bar-driven, so
    # a stream that carries only per-file events still shows live speed (2026-10-23).

    # Per-file rows are the source of truth for bytes + finished count (sequential rework).
    # Sweep FIRST, then sum: a swept (cached/fast) row that never opened a bar now carries
    # its real size, so the Total line lands on 100% instead of stalling mid-repo.
    _sweep_ghost_rows(model_state)
    _recompute_totals(model_state)
    _update_stage_speed(model_state, ev.get("speed_bps"))
    model_state["current_file"] = _pick_current_file(model_state)


def _ensure_model_cached(rt: dict, model_ids=None) -> bool:
    """Prepare the HF model cache(s) BEFORE the generation subprocess starts (2026-09-12).

    *model_ids* may be a single id or a LIST - every repo is downloaded in ONE prep-script
    run with SEQUENTIAL per-file downloads (max_workers=1, 2026-10-05) and ONE shared
    progress state, so the UI shows continuous statistics from the first file of the first
    repo to the last file of the last repo - no restart at zero between repos.

    WHY THIS EXISTS - client-machine field failure: on a FRESH machine the first run
    must download the tier's weights (~80 GB for the high tier's two Qwen repos, ~17 GB
    for mid Z-Image) into
    <runtime_dir>/hf_cache. Doing that inside the worker burned the whole subprocess
    timeout and reported a misleading 'generation timed out'. The pre-flight now gets its
    own generous budget (TOOLS_BOOTSTRAP_TIMEOUT_SEC) and is resumable; when everything is
    already cached it finishes in a couple of seconds.

    LIVE PROGRESS (2026-09-21, sequential rework 2026-10-05): the script's stdout is
    STREAMED line-by-line; per-file JSON events are merged into <runtime_dir>/
    .setup_progress.json stage 'model'. The state CARRIES across repos and retries - a
    done file stays done, so an interrupted download visibly resumes where it stopped
    instead of resetting every row to zero (the old per-repo reset() was the cause).

    LAZY STAGE (2026-09-24): the 'model' row is written only when a REAL progress event
    arrives. A fully-cached pre-flight emits no events and therefore no row - the UI's
    Setup/Downloads section stays invisible on machines where everything is available.

    NON-FATAL by design: the worker itself falls back to a download inside its own
    per-tier subprocess budget, so a slow/failed pre-flight only costs one retry. Returns
    True when every requested model is cached locally.
    """
    ids = ([str(m) for m in model_ids if str(m)] if isinstance(model_ids, (list, tuple))
           else [str(model_ids)] if model_ids else None)
    _migrate_legacy_model_cache(rt)
    cmd = [rt["python_exe"], "-c", _model_prep_script(ids or [_MODEL_ID])]

    progress = _setup_progress(rt)
    # current_file starts as an explicit None - the manifest branch returns early
    # without touching it, and a missing key would make the UI's pickKey undefined.
    model_state: dict = {"status": "running", "current_file": None}
    stage_started = [False]  # lazy write - see the LAZY STAGE note in this docstring

    def _reclaim_previous() -> None:
        """Re-adopt the previous attempt's file rows ONCE, before any merge.

        2026-10-05: NO progress.reset() anywhere - a fresh generation is claimed by the
        SetupProgress constructor itself (it bumps the file's generation once per run), and
        this makes resume visible from second 0: rows with real bytes (_seen) are copied
        into the fresh state by stable row key, so when the first manifest event merges,
        its arc sums already include "X GB on disk" and done flags - finished files cannot
        look like they restarted at zero. A fresh machine has no previous rows: no-op.
        """
        if progress is None or prev_state[0] is not None:
            return
        try:
            prev = progress.stage("model") or {}
        except Exception:
            prev = {}
        if not isinstance(prev, dict):
            return
        # NOTE (2026-10-23 audit fix): only FILE ROWS are carried over - never the stage-level
        # counters (total_size/downloaded/repo_index/repo_count). Those describe the PREVIOUS
        # attempt's repo arc; copying them made a mid-repo-2 retry show "60 GB / 40 GB" (>100%)
        # when repo 2's manifest reset total_size to its own arc. Row bytes are re-summed by each
        # repo's manifest branch instead (rows carry their _repo tags), so resume is still visible
        # from second 0 and the live numbers always describe exactly ONE repo.
        pf = prev.get("files")
        if isinstance(pf, dict):
            files = model_state.setdefault("files", {})
            for key, pv in pf.items():
                if not isinstance(pv, dict) or not pv.get("_seen"):
                    continue  # manifest-only rows have no real progress to carry
                # filename must stay the BARE path - the row key is '<repo>:<path>', and a
                # prefixed name would leak into the UI Current line on retry (2026-10-23 audit).
                bare = str(key).split(":", 1)[1] if ":" in str(key) else str(key)
                target = files.setdefault(str(key), {"filename": bare})
                for fk in ("downloaded", "size"):
                    fv = pv.get(fk)
                    if isinstance(fv, (int, float)) and fv > int(target.get(fk) or 0):
                        target[fk] = int(fv)
                for fk in ("done", "_seen"):
                    if pv.get(fk):
                        target[fk] = True
                # carry the repo tag (2026-10-23 audit fix): an untagged row is treated as
                # CURRENT-repo by _repo_rows(), so on a retry every reclaimed row would be
                # counted into this run's first arc - including rows of OTHER repos.
                if pv.get("_repo") is not None:
                    target["_repo"] = pv["_repo"]

    prev_state = [None]  # set on first read so the re-claim runs exactly once

    def _write_model_stage() -> None:
        """Persist the model stage on FIRST real event (lazy row - see LAZY STAGE note)."""
        if progress is None or stage_started[0]:
            return
        stage_started[0] = True
        try:
            if prev_state[0] is None:  # defensive - _on_line already re-claims first
                _reclaim_previous()
                prev_state[0] = True
            progress.set_stage("model", **model_state)
        except Exception:
            pass

    last_write = [0.0]

    def _on_line(line: str):
        ev, _contract = _parse_model_progress_line(line)
        if not ev or progress is None:
            return
        try:
            # Re-claim the previous attempt's rows ONCE, before ANY merge - the first
            # manifest event must already see "X GB on disk" in its arc sums.
            if prev_state[0] is None:
                _reclaim_previous()
                prev_state[0] = True
            _merge_model_progress(model_state, ev)
            # First REAL event = the pre-flight genuinely has work to do (a manifest with
            # files to fetch / a download bar). Only NOW does the model stage appear in
            # .setup_progress.json - a fully-cached run emits no events and shows nothing.
            _write_model_stage()
            now = _time.time()
            if now - last_write[0] > 0.5:   # throttle: ~2 writes/second max
                last_write[0] = now
                progress.set_stage("model", **model_state)
        except Exception as e:  # informational only - never break the download
            logger.debug(f"  model progress update failed (ignored): {e}")

    try:
        # Resolve the shared streaming runner the same way every other tool_bootstrap
        # symbol is resolved: injected exec global on the CLIENT, real package import in
        # local dev. A bare call to run_streaming() is a NameError outside the CLIENT
        # (2026-10-23: the model pre-flight could never run in local dev because of it).
        stream_fn = globals().get("run_streaming") or _tb_symbol("run_streaming")  # noqa: B009
        if stream_fn is None:
            logger.warning("  Model pre-flight skipped: run_streaming is unavailable")
            return False
        rc, out_lines, err_text = stream_fn(cmd, timeout=_bootstrap_timeout(),
                                            env=_tool_env(rt), on_line=_on_line)
    except subprocess.TimeoutExpired:
        if progress is not None and stage_started[0]:
            model_state["status"] = "error"
            model_state["detail"] = f"timed out after {_bootstrap_timeout()}s (partial download resumes)"
            try:
                progress.set_stage("model", **model_state)
            except Exception:
                pass
        logger.warning(
            f"  Model pre-flight timed out after {_bootstrap_timeout()} s - the worker "
            f"will retry with its own (larger) budget; partial downloads resume."
        )
        return False

    out = "\n".join(out_lines).strip()
    if rc == 0 and "MODEL_READY" in out:
        logger.info(f"  Model cache ready ({out.splitlines()[-1]})")
        if progress is not None and stage_started[0]:
            # Final reconciliation: MODEL_READY means snapshot_download returned for every
            # requested repo - any row that never opened a bar (cached/fast file) is done.
            try:
                _sweep_ghost_rows(model_state, final=True)
                _recompute_totals(model_state)  # swept rows now hold their real sizes
                model_state["current_file"] = None
            except Exception:
                pass  # informational only - the state file keeps whatever it has
            model_state["status"] = "done"
            try:
                progress.set_stage("model", **model_state)
            except Exception:
                pass
        # stage_started[0] is False here when the cache was already complete: no events,
        # no row written - the UI never shows a 'Downloading model files' section for it.
        return True

    detail = ((out + "\n" + (err_text or "")).strip()).splitlines()
    tail = detail[-1][:400] if detail else f"exit code {rc}"
    logger.warning(f"  Model pre-flight incomplete ({tail}) - the worker will attempt "
                   f"a download inside its own subprocess budget")
    if progress is not None and stage_started[0]:
        model_state["status"] = "error"
        model_state["detail"] = tail[:300]
        try:
            progress.set_stage("model", **model_state)
        except Exception:
            pass
    return False

def _active_manifest() -> dict:
    """The SERVER-shipped TOOL_MANIFEST global when present, else the in-file default."""
    m = globals().get("TOOL_MANIFEST")  # noqa: B009 - injected symbol by design
    if isinstance(m, dict) and m.get("runtime_name"):
        return m
    return _DEFAULT_MANIFEST


def _bootstrap_timeout() -> int:
    """First-run setup budget from the SERVER-shipped constant (injected global).

    Falls back to the same 3 h value as config.TOOLS_BOOTSTRAP_TIMEOUT_SEC: a fresh
    high-tier machine must pull BOTH Qwen repos (~40 GB each), so a 40-minute budget
    would abort a download that is working perfectly.
    """
    v = globals().get("TOOLS_BOOTSTRAP_TIMEOUT_SEC")  # noqa: B009
    if isinstance(v, int) and v > 0:
        return v
    return 10800


def generate_image(
    prompt: str,
    height: int = None,
    width: int = None,
    num_inference_steps: int = 9,
    guidance_scale: float = 0.0,
    output_filename: str = "generated_image",
    reference_image: str = None,
    strength: float = 0.6
) -> dict:
    """Generate an image via the GPU-adaptive pipeline (subprocess in the tool venv).

    PROBES THE CLIENT'S NVIDIA GPU FIRST and picks the tier-appropriate model files +
    pipeline mode so any card from a 6 GB laptop 3060 to a 5090 runs comfortably:

        high (>= 24 GB): Qwen/Qwen-Image FP8 + cpu offload (v21); img2img via the separate
                         Qwen/Qwen-Image-Edit repo (instruction-based, no strength).
        mid  (12-23.9) : Z-Image-Turbo FP8 (~9 guidance-free steps; ~17 GB peak resident).
        low  (< 12 GB) : int8 SANA-Sprint 0.6B (v9, ~5 GB download; dequantized to the
                         same bf16 tensors at load) + cpu offload, resolution-clamped.

    DEFAULT RESOLUTION (2026-10-03): when width/height are omitted (None) they come from
    the user's Settings > Runtime tab ('image_gen_width'/'image_gen_height' in the CLIENT
    settings.json - read LIVE on every call, default 600x400); an explicit caller value
    always wins. The worker still snaps to tier-safe multiples (32 on high / Qwen-Image
    and low / SANA-Sprint, 16 on mid / Z-Image) and clamps to the tier's max_pixels
    budget either way.

    The user can pin any line regardless of hardware via config IMAGE_GEN_TIER_OVERRIDE
    ('auto'|'high'|'mid'|'low') - see the module docstring for semantics and safety.

    Returns dict with status/filename/file_path/image_base64/width/height/message.
    """
    try:
        from ..utils import get_working_root, is_image_file, validate_filename
        from ..path_guard import validate_file_access
        # Generic self-unpacking bootstrap (shared module tools.tool_bootstrap):
        # rewritten by the loader to 'ensure_tool_runtime = tool_bootstrap.ensure_tool_runtime'.
        from ..tool_bootstrap import ensure_tool_runtime

        # (2026-10-03) Default resolution from Settings > Runtime tab: an explicit caller
        # value wins; otherwise the per-machine pick in settings.json applies LIVE.
        if width is None or height is None:
            client_res = _client_image_gen_resolution()
            if client_res:
                cw, ch = client_res
                width = cw if width is None else width
                height = ch if height is None else height
        width = int(width) if width is not None else 600
        height = int(height) if height is not None else 400
        # Tool calls routinely send null for optional numbers (`"num_inference_steps": null`).
        # Normalize ONCE here: the worker would otherwise pass None straight into a pipeline
        # and fail with an opaque TypeError deep inside diffusers (2026-10-23).
        try:
            num_inference_steps = int(num_inference_steps)
        except (TypeError, ValueError):
            num_inference_steps = 9
        try:
            guidance_scale = float(guidance_scale)
        except (TypeError, ValueError):
            guidance_scale = 0.0

        logger.info("generate_image called - starting subprocess execution")
        logger.info(f"  prompt: {prompt}")
        logger.info(f"  height: {height}, width: {width}")
        logger.info(f"  num_inference_steps: {num_inference_steps}, guidance_scale: {guidance_scale}")
        logger.info(f"  output_filename: {output_filename}")

        # GPU probe -> tier (2026-09-12): pick the model files + pipeline mode that fit
        # this machine. No NVIDIA GPU / unreadable nvidia-smi -> 'high' = legacy path,
        # so behavior on unknown hardware is exactly what it was before tiering.
        gpu_info = _probe_gpu()
        auto_tier = _classify_tier(gpu_info)
        # (2026-09-20 user-defined override): config IMAGE_GEN_TIER_OVERRIDE pins the model
        # line so a big card can deliberately run the smaller/faster one. Applied only when a
        # GPU was actually probed - unknown hardware keeps the legacy 'high' path exactly as
        # before tiering, and an unaffordable pin falls back to the auto tier (no OOM).
        tier = auto_tier
        pinned = _tier_override() if gpu_info else ""
        if pinned and pinned != "auto":
            min_vram = TIER_MIN_VRAM_MIB.get(pinned, 0)
            if int(gpu_info.get("vram_mib") or 0) < min_vram:
                logger.warning(
                    f"  IMAGE_GEN_TIER_OVERRIDE={pinned!r} needs >= {min_vram // 1024} GB VRAM but this "
                    f"GPU has {gpu_info.get('vram_mib', 0)} MiB - falling back to auto tier '{auto_tier}'")
            else:
                tier = pinned
                if tier != auto_tier:
                    logger.info(f"  IMAGE_GEN_TIER_OVERRIDE={tier!r} overrides auto classification "
                                f"(auto would have picked '{auto_tier}')")
        cfg = _tier_config_for(tier)

        # v21: Qwen/Qwen-Image is tuned for ~50 FlowMatchEuler steps; the tool schema ships
        # 9 (a Z-Image-turbo number), so when the caller left the default in place we swap
        # in the qwen trajectory. An explicit user step count always wins - detectable by
        # any value other than the schema default of 9.
        if str(cfg.get("pipeline") or "") == "qwen_image" and int(num_inference_steps) == 9:
            num_inference_steps = 50
        # v9 publish gate: while our int8 SANA-Sprint 0.6B repo is unpublished (placeholder
        # id), the low tier runs the bf16 0.6B instead (~7.7 GB, NOT the 9.7 GB 1.6B - audit fix).
        if cfg.get("dtype") == "int8" and not _int8_model_ready(cfg["model_id"]):
            logger.info(f"  int8 SANA-Sprint 0.6B repo not published yet ({cfg['model_id']!r}) - "
                        f"low tier falls back to bf16 {_SANA_06_MODEL_ID}")
            cfg = dict(cfg)
            cfg["model_id"] = _SANA_06_MODEL_ID
            cfg["dtype"] = "bf16"

        # v21 mid tier: Z-Image-Turbo FP8 resident peaks ~17 GB - probed VRAM below ~18 GB
        # gets cpu offload added so 12/16 GB cards run it without OOM (manifest promise).
        if str(cfg.get("pipeline") or "") == "zimage" and gpu_info \
                and int(gpu_info.get("vram_mib") or 0) < 18 * 1024:
            cfg = dict(cfg)
            cfg["offload"] = True
            logger.info(f"  Mid tier on {gpu_info['name']} (< 18 GB VRAM): cpu offload added")
        if gpu_info:
            logger.info(f"  GPU probe: {gpu_info['name']} | "
                        f"{gpu_info.get('vram_mib', 0)} MiB VRAM | sm_{gpu_info.get('compute_cap', '?')}")
        else:
            logger.warning("  GPU probe found no NVIDIA GPU - using the legacy bf16 path")
        logger.info(f"  Tier '{tier}': pipeline={cfg.get('pipeline', 'zimage')} "
                    f"model={cfg['model_id']} dtype={cfg['dtype']} "
                    f"offload={bool(cfg.get('offload'))} max_pixels={cfg.get('max_pixels')}")

        # Ensure filename doesn't have extension.
        if output_filename.endswith('.png'):
            output_filename = output_filename[:-4]

        # SECURITY (2026-07-15): user-supplied output filenames may contain
        # separators or '..' -- validate before passing to the worker subprocess.
        try:
            validate_filename(output_filename, label="Output filename")
        except ValueError as e:
            return {
                "status": "error",
                "filename": None,
                "file_path": None,
                "image_base64": None,
                "width": width,
                "height": height,
                "message": f"Invalid output filename: {e}"
            }

        # Unfold this tool's own runtime on the user machine (idempotent - reuses
        # the venv/worker from previous iterations; first run pays setup cost).
        try:
            rt = ensure_tool_runtime(
                _active_manifest(),
                worker_source=WORKER_SOURCE,
                worker_filename=_WORKER_FILENAME,
                bootstrap_timeout=_bootstrap_timeout(),
            )
        except Exception as e:
            logger.error(f"[generate_image] Runtime bootstrap failed: {e}")
            return {
                "status": "error",
                "filename": None,
                "file_path": None,
                "image_base64": None,
                "width": width,
                "height": height,
                "message": f"Tool runtime setup failed: {e}"
            }

        # Prepare the tier's model cache with its OWN budget BEFORE generation (2026-09-12).
        # First run on a fresh machine downloads the tier's repos into <runtime_dir>/hf_cache
        # - ~80 GB for the high tier (Qwen/Qwen-Image + Qwen/Qwen-Image-Edit, bf16 weights
        # stored as fp8 at load), ~17 GB for mid (Z-Image fp8), ~7.7 GB for low (SANA bf16).
        # Later runs are a couple of seconds. Non-fatal: the worker has its own per-tier
        # download budget as a net.
        try:
            # High tier needs TWO repos (base + img2img Edit) - pass them as a list so ONE
            # prep-script run downloads both sequentially with one shared progress state.
            model_ids = [cfg["model_id"]]
            edit_id = cfg.get("edit_model_id")
            if isinstance(edit_id, str) and edit_id.strip() and edit_id != cfg["model_id"]:
                model_ids.append(edit_id)
            _ensure_model_cached(rt, model_ids)
        except Exception as e:
            logger.warning(f"  Model preparation failed ({e}) - the worker will attempt "
                           f"a download inside its own subprocess budget")

        # Build task JSON - include working_root + tier config for the subprocess.
        effective_working = get_working_root()
        task = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale,
            "output_filename": output_filename,
            "working_root": effective_working,
            # GPU tier choice (2026-09-12 / v7 2026-09-13): which pipeline + files to load and how.
            "pipeline": cfg.get("pipeline", "zimage"),
            "model_id": cfg["model_id"],
            # v21 high tier img2img: separate instruction-based Edit repo.
            "edit_model_id": cfg.get("edit_model_id"),
            "dtype": cfg.get("dtype", "bf16"),
            "offload": bool(cfg.get("offload")),
            "max_pixels": cfg.get("max_pixels"),
        }

        # Determine mode - SECURE: validate reference_image inside working_root before use.
        if reference_image:
            try:
                validated_ref = validate_file_access(
                    reference_image,
                    effective_working,
                    label="Reference image"
                )
                if not is_image_file(validated_ref):
                    logger.warning(f"  Reference path is not a valid image: {validated_ref}")
                    mode = "text2img"
                else:
                    mode = "img2img"
                    task["reference_image"] = validated_ref
                    task["strength"] = strength
                    logger.info(f"  Mode: image-to-image with reference: {validated_ref}")
            except (ValueError, FileNotFoundError) as e:
                logger.warning(f"  Reference image rejected by path guard: {e}")
                mode = "text2img"
        else:
            mode = "text2img"

        if mode == "text2img":
            logger.info("  Mode: text-to-image")

        # Build command against the SELF-UNPACKED runtime (never __file__ paths).
        cmd = [rt["python_exe"], rt["worker_script"], f"--mode={mode}"]
        timeout_s = _effective_timeout(cfg)
        if cfg.get("offload"):
            logger.info(f"  Offloaded tier: subprocess budget {timeout_s}s (vs "
                        f"{IMAGE_SUBPROCESS_TIMEOUT}s resident)")
        logger.info(f"  Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                input=json.dumps(task),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_s,
                env=_tool_env(rt),
                text=True
            )
        except subprocess.TimeoutExpired:
            # The model cache was already prepared before this step (2026-09-12), so a
            # timeout here means the GENERATION itself was too slow - typically CPU-only.
            error_msg = (
                f"Image generation timed out ({timeout_s} seconds). The model "
                f"cache was ready, so this usually means a CPU-only machine or an unusually "
                f"slow GPU/disk. Tier '{tier}' used pipeline {cfg.get('pipeline', 'zimage')} "
                f"model {cfg['model_id']} (dtype={cfg.get('dtype')}, offload={bool(cfg.get('offload'))}). "
                f"Check that the tool venv can use CUDA (torch.cuda.is_available())."
            )
            logger.error(f"  Timeout: {error_msg}")
            return {
                "status": "error",
                "filename": None,
                "file_path": None,
                "image_base64": None,
                "width": width,
                "height": height,
                "message": error_msg
            }

        if result.returncode != 0:
            # The worker reports its real error as a JSON line on STDOUT (stderr is usually
            # empty) - parse it first so the actual cause reaches the user instead of an
            # opaque 'Unknown subprocess error' (2026-09-12 client fix).
            error_msg = None
            try:
                candidate = json.loads(result.stdout.strip().splitlines()[-1])
                if isinstance(candidate, dict) and candidate.get("status") == "error":
                    error_msg = str(candidate.get("message") or "")[:2000]
            except (json.JSONDecodeError, IndexError):
                pass
            if not error_msg:
                error_msg = result.stderr.strip()[-2000:] if result.stderr else "Unknown subprocess error"
            logger.error(f"  Subprocess error: {error_msg}")
            return {
                "status": "error",
                "filename": None,
                "file_path": None,
                "image_base64": None,
                "width": width,
                "height": height,
                "message": error_msg
            }

        try:
            response = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            error_msg = f"Failed to parse worker output: {str(e)}\n{result.stdout[-500:]}"
            logger.error(f"  {error_msg}")
            return {
                "status": "error",
                "filename": None,
                "file_path": None,
                "image_base64": None,
                "width": width,
                "height": height,
                "message": error_msg
            }

        if response.get("status") == "ok":
            logger.info(f"  \u2713 Image generated successfully: {response.get('filename')}")
            logger.info(f"  \u2713 File path: {response.get('file_path')}")
            logger.info(f"  \u2713 Image size: {len(response.get('image_base64', ''))} bytes (base64)")
        else:
            logger.error(f"  Worker reported error: {response.get('message')}")

        return response

    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(f"  {error_msg}")
        import traceback
        logger.debug(traceback.format_exc())
        return {
            "status": "error",
            "filename": None,
            "file_path": None,
            "image_base64": None,
            "width": width,
            "height": height,
            "message": error_msg
        }
