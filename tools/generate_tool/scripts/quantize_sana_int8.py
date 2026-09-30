"""One-time builder: int8-quantized SANA-Sprint diffusers repo (v9, 2026-09-24).

WHY THIS EXISTS
---------------
The low tier (< 12 GB VRAM) of the generate_image tool used to download NVIDIA's
bf16 SANA-Sprint repos - the 1.6B one is ~9.7 GB (half of which is the Gemma-2-2B text
encoder in bf16), the 0.6B one ~7.7 GB. This script builds a drop-in replacement
diffusers-format folder where every nn.Linear weight (text_encoder + transformer) is
stored as TRUE int8 tensors with a per-channel scale sidecar:

    bf16 1.6B repo  ~9.7 GB   ->   int8 build  ~5-6 GB on disk / download
    bf16 0.6B repo  ~7.7 GB   ->   int8 build  ~4.5-5 GB (v9 LOW TIER default)

    (VAE + tokenizer are copied verbatim at ~1.3 GB - the VAE is the hard floor; Gemma's
    embedding table stays bf16, only nn.Linear weights become int8. The script prints the
    exact final size.)

How it works at runtime (see _dequantize_int8_linears in generate_image.py): the
worker loads the pipeline through the NORMAL bf16 path (int8 tensors pass through
from_pretrained uncast) and dequantizes each Linear weight in memory at load time
(w = w_int * scale, back to bf16). So generation runs on exactly the same bf16
tensors of the matching bf16 SANA-Sprint - same VRAM peak (0.6B: ~4.3 GB offloaded,
same speed. The win is purely download size + disk footprint - which is what this
build optimizes for.

Why NOT bitsandbytes Int8Linear: bnb keeps the ORIGINAL float weights in memory and
applies int8 math only on-the-fly during forward - its saved state dict serializes
as float32, i.e. LARGER than bf16. True int8-on-disk requires storing the quantized
tensors themselves (this script) plus a dequant step at load time (the worker).

WHAT IT DOES
------------
1. snapshot_download() of the bf16 source repo into its own HF_HOME cache dir
   (default: <CLIENT_DIR>/tools/runtimes/generate_tool/hf_cache - same place client
   weights live; when run outside a client tree, next to the output folder).
2. Loads text_encoder (Gemma2Model) + transformer (SanaTransformer2DModel), converts
   every nn.Linear weight to int8 (symmetric per-channel: scale = max|w| / 127), saves
   the modules back as safetensors with their configs - plus one scales.safetensors
   sidecar per module holding <param_name>.scale tensors.
3. Copies everything else VERBATIM: VAE (bf16 - precision-critical and small),
   tokenizer, scheduler config, model_index.json, README/LICENSE.

OUTPUT LAYOUT (a complete SanaSprintPipeline.from_pretrained() folder)
----------------------------------------------------------------------
    <dest>/model_index.json            (unchanged copy)
    <dest>/text_encoder/  int8 shards + configs + index.json + scales.safetensors
    <dest>/transformer/   diffusion_pytorch_model.safetensors (int8) + config.json
                          + scales.safetensors
    <dest>/vae/           bf16, unchanged
    <dest>/tokenizer/     unchanged
    <dest>/scheduler/     unchanged

USAGE
-----
    python quantize_sana_int8.py [--size {1.6|0.6}] [--source REPO] [--dest DIR]

Defaults (per --size): 1.6 -> Sana_Sprint_1.6B_1024px_diffusers / sana_sprint_1.6b_int8
                        0.6 -> Sana_Sprint_0.6B_1024px_diffusers / sana_sprint_0.6b_int8
          (dest under <CLIENT_DIR>/tools/runtimes/generate_tool/hf_cache/ - the same place
           client weights live; COOLEMS_CLIENT_ROOT env var, else repo-relative CLIENT/)

After it finishes: publish <dest> as a Hugging Face model repo (Apache-2.0 base),
then set the matching constant in tools/generate_tool/generate_image.py
(_SANA_INT8_06_MODEL_ID for --size 0.6, _SANA_INT8_16_MODEL_ID for 1.6) so the low tier picks it up on its next run.

NOTE ON QUALITY: 2-step distilled models are more quantization-sensitive than
normal ones. Per-channel int8 of Linear weights dequantizes to a near-bf16 tensor,
so expected loss is small - but A/B a handful of prompts int8 vs bf16 before
shipping (the tool reports model_loaded=int8|bf16 in its result JSON for exactly this).
"""

import argparse
import os
import shutil
import sys


SRC_16 = "Efficient-Large-Model/Sana_Sprint_1.6B_1024px_diffusers"
SRC_06 = "Efficient-Large-Model/Sana_Sprint_0.6B_1024px_diffusers"


def default_dest(size: str = "0.6") -> str:
    client_root = os.environ.get("COOLEMS_CLIENT_ROOT") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "CLIENT")
    suffix = f"sana_sprint_{size}b_int8"
    return os.path.normpath(os.path.join(
        client_root, "tools", "runtimes", "generate_tool", "hf_cache", suffix))


def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.2f} {unit}"
        n /= 1024.0


def _quantize_linears_inplace(module, scales: dict):
    """Convert every nn.Linear weight to int8 (symmetric per-channel) in place.

    w_int = round(w / scale),  scale = max|w| / 127   (per output channel).
    Scales are recorded as <full_param_name>.scale for the sidecar file; each Linear's
    own weight parameter is replaced by the int8 tensor, so save_pretrained() writes
    true int8 on disk. Non-Linear parameters (norms/embeddings) stay bf16 - the worker
    casts them to the compute dtype at load time. Returns the number of converted layers.
    """
    import torch

    import torch.nn as nn

    count = 0
    for mod_name, mod in module.named_modules():
        if not isinstance(mod, nn.Linear):
            continue
        w = mod.weight
        if w is None or w.dtype == torch.int8:
            continue
        full = f"{mod_name}.weight" if mod_name else "weight"
        scale = w.abs().amax(dim=1).float() / 127.0
        scale = torch.clamp(scale, min=1e-8)
        w_int = torch.round(w.to(torch.float32) / scale.unsqueeze(1)).to(torch.int8)
        mod._parameters["weight"] = nn.Parameter(w_int, requires_grad=False)
        scales[full + ".scale"] = scale.contiguous()
        count += 1
    return count


def quantize_module(src_dir: str, subfolder: str, dst_dir: str, cls_name: str):
    """Load one model class from the bf16 snapshot, int8-ify its Linears, save to dst_dir."""
    import torch

    # Import lazily so --help works without the heavy deps installed.
    if cls_name == "Gemma2Model":
        from transformers import Gemma2Model as Cls
        model = Cls.from_pretrained(os.path.join(src_dir, subfolder),
                                    torch_dtype=torch.bfloat16, low_cpu_mem_usage=True)
    else:  # SanaTransformer2DModel
        from diffusers import SanaTransformer2DModel as Cls
        model = Cls.from_pretrained(src_dir, subfolder=subfolder,
                                    torch_dtype=torch.bfloat16, low_cpu_mem_usage=True)

    scales = {}
    n_linear = _quantize_linears_inplace(model, scales)

    os.makedirs(dst_dir, exist_ok=True)
    model.save_pretrained(dst_dir, safe_serialization=True, max_shard_size="1500MB")

    # Scales sidecar (float32 - tiny: one float per output channel of each Linear).
    from safetensors.torch import save_file
    scale_state = {k: v.contiguous() for k, v in scales.items()}
    if scale_state:
        save_file(scale_state, os.path.join(dst_dir, "scales.safetensors"), metadata={"format": "pt"})

    # Sanity report: dtypes that actually landed on disk.
    from safetensors import safe_open
    dtypes = {}
    for f in sorted(os.listdir(dst_dir)):
        if not f.endswith(".safetensors") or f == "scales.safetensors":
            continue
        with safe_open(os.path.join(dst_dir, f), framework="pt") as sf:
            for k in sf.keys():
                t = str(sf.get_slice(k).get_dtype())
                dtypes[t] = dtypes.get(t, 0) + 1
    print(f"  {subfolder}: {n_linear} Linear layers -> int8 | on-disk dtypes: {dtypes}")
    if not any(d.startswith("int8") for d in dtypes):
        raise RuntimeError(f"{subfolder}: no int8 tensors saved - quantization failed!")


def copy_static_files(src_dir: str, dst_dir: str):
    """Everything that is NOT a (re)quantized weight file - verbatim copies."""
    skipped = 0
    for root, _dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        for f in files:
            if rel_root == ".":
                keep = True  # model_index.json, README.md, LICENSE, .gitattributes
            elif rel_root in ("text_encoder", "transformer"):
                # configs stay; weight shards are replaced by save_pretrained's int8 ones.
                # *.index.json is ALSO skipped here: save_pretrained already wrote the
                # correct shard map for the INT8 files - copying the SOURCE index over it
                # would point at bf16 shard names that no longer exist (load failure).
                keep = not f.endswith(".safetensors") and not f.endswith(".index.json")
            else:  # vae/, tokenizer/, scheduler/ - untouched, byte-identical
                keep = True
            if not keep:
                skipped += 1
                continue
            dest_full = os.path.join(dst_dir, f) if rel_root == "." \
                else os.path.join(dst_dir, rel_root, f)
            os.makedirs(os.path.dirname(dest_full), exist_ok=True)
            shutil.copy2(os.path.join(root, f), dest_full)
    print(f"  copied static files (skipped {skipped} bf16 weight shards)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--size", choices=["1.6", "0.6"], default="0.6",
                    help="which SANA-Sprint to quantize (v9 low tier uses 0.6)")
    ap.add_argument("--source", default=None,
                    help="HF repo id of the bf16 SANA-Sprint diffusers model"
                         "(default per --size; or a local snapshot with --skip-download)")
    ap.add_argument("--dest", default=None, help="output folder for the int8 build")
    ap.add_argument("--skip-download", action="store_true",
                    help="--source is a LOCAL snapshot dir (already downloaded)")
    args = ap.parse_args()

    source = args.source or (SRC_16 if args.size == "1.6" else SRC_06)
    dest = os.path.abspath(args.dest or default_dest(args.size))
    if os.path.exists(dest):
        ans = input(f"Destination {dest} already exists - delete and rebuild? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return 1
        shutil.rmtree(dest, ignore_errors=True)

    # 1) bf16 source snapshot (resumable). Inside a client tree it lands in the tool's
    #    own hf_cache; on this dev machine it lands next to the output folder.
    if args.skip_download:
        src_dir = os.path.abspath(source)
        if not os.path.isdir(os.path.join(src_dir, "transformer")):
            print(f"ERROR: --skip-download expects a local snapshot at {src_dir}")
            return 1
    else:
        from huggingface_hub import snapshot_download
        client_root = os.environ.get("COOLEMS_CLIENT_ROOT") or ""
        if client_root and dest.startswith(os.path.abspath(client_root)):
            cache_home = os.path.normpath(os.path.join(
                client_root, "tools", "runtimes", "generate_tool", "hf_cache"))
        else:
            cache_home = os.path.join(os.path.dirname(dest), "hf_cache")
        print(f"[1/4] snapshot_download {source} -> HF_HOME={cache_home}")
        src_dir = snapshot_download(source, cache_dir=cache_home)

    before_te = dir_size(os.path.join(src_dir, "text_encoder"))
    before_tf = dir_size(os.path.join(src_dir, "transformer"))

    print(f"[2/4] quantizing text_encoder (bf16 {human(before_te)}) ...")
    quantize_module(src_dir, "text_encoder", os.path.join(dest, "text_encoder"), "Gemma2Model")

    print(f"[3/4] quantizing transformer (bf16 {human(before_tf)}) ...")
    quantize_module(src_dir, "transformer", os.path.join(dest, "transformer"), "SanaTransformer2DModel")

    print("[4/4] copying static files (VAE bf16, tokenizer, scheduler, configs) ...")
    copy_static_files(src_dir, dest)

    after_te = dir_size(os.path.join(dest, "text_encoder"))
    after_tf = dir_size(os.path.join(dest, "transformer"))
    print("\n=== SIZE REPORT ===")
    print(f"  text_encoder : {human(before_te):>10} -> {human(after_te):>10}")
    print(f"  transformer  : {human(before_tf):>10} -> {human(after_tf):>10}")
    total = dir_size(dest)
    print(f"  TOTAL OUTPUT : {human(total)}   (bf16 1.6B repo ~9.7 / 0.6B repo ~7.7 GB)")

    print(f"\nOK - int8 SANA-Sprint build ready at:\n  {dest}\n"
          "Next: publish this folder as an HF model repo (Apache-2.0 base license),\n"
          "then set _SANA_INT8_06_MODEL_ID / _SANA_INT8_16_MODEL_ID in generate_image.py accordingly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
