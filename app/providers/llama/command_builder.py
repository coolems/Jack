"""Command line builder for llama-server process startup.

Constructs the full command list with all flags from config, including
KV cache optimization, flash attention, reasoning mode, MTP speculative
decoding, and vision projector setup.
"""

import logging
from typing import Optional

logger = logging.getLogger("COOLEMS.Provider.Llama.Server")

# Import all server configuration from config module
from config import (
    CONTEXT_WINDOW_TOKENS,
    LLAMA_SERVER_PORT,
    LLAMA_SERVER_THREADS,
    LLAMA_SERVER_GPU_LAYERS,
    # KV Cache
    LLAMA_SERVER_CACHE_TYPE_K,
    LLAMA_SERVER_CACHE_TYPE_V,
    LLAMA_SERVER_CACHE_RAM,
    # Inference Flags
    LLAMA_SERVER_FLASH_ATTN,
    LLAMA_SERVER_PARALLEL,
    LLAMA_SERVER_NO_MMAP,
    LLAMA_SERVER_NO_WARMUP,
    LLAMA_SERVER_JINJA,
    LLAMA_SERVER_FIT,
    # Server-Level Generation Defaults
    LLAMA_SERVER_TEMP,
    LLAMA_SERVER_REPEAT_PENALTY,
    # Reasoning / Thinking Mode
    LLAMA_SERVER_REASONING,
    LLAMA_SERVER_REASONING_BUDGET,
    LLAMA_SERVER_PRESERVE_THINKING,
    # MTP (Multi-Token Prediction) Support
    LLAMA_SERVER_MTP_ENABLED,
    LLAMA_SERVER_SPEC_TYPE,
    LLAMA_SERVER_SPEC_DRAFT_N_MAX,
    LLAMA_SERVER_SPEC_DRAFT_N_MIN,
    LLAMA_SERVER_SPEC_DRAFT_P_SPLIT,
    LLAMA_SERVER_SPEC_DRAFT_P_MIN,
    # Max Output Tokens
    LLAMA_SERVER_MAX_NEW_TOKENS,
)


def _build_server_cmd(llama_server_exe: str, model_file: str,
                      mmproj_file: Optional[str] = None,
                      enable_mtp: bool = False,
                      host: str = "0.0.0.0",
                      port: int = LLAMA_SERVER_PORT,
                      parallel: int = LLAMA_SERVER_PARALLEL,
                      ctx_per_slot: Optional[int] = None,
                      threads: Optional[int] = None,
                      gpu_layers: Optional[int] = None,
                      extra_args: Optional[list] = None) -> list:
    """Build the llama-server command with optimal parameters for Qwen3.6.
    All parameters come from config.py — modify there, not here.
    MTP provides >2x speedup with ~75% token acceptance rate.
    """
    slots = max(1, int(parallel or 1))
    ctx_per_slot = int(ctx_per_slot) if ctx_per_slot else CONTEXT_WINDOW_TOKENS
    ctx_total = ctx_per_slot * slots
    n_threads = int(threads) if threads else LLAMA_SERVER_THREADS
    n_gpu_layers = int(gpu_layers) if gpu_layers is not None else LLAMA_SERVER_GPU_LAYERS
    cmd = [
        llama_server_exe,
        # (2026-09-09 fresh-start fix) CRITICAL: the model path MUST be passed here.
        # Without --model this build of llama-server boots in "router mode" and loads
        # NOTHING at startup ("models will be automatically loaded on-demand") - /health
        # answers 200 but /v1/models stays empty forever, so start_server reported
        # success with 0 models and every model switch deadlocked in the reload loop.
        "--model", model_file,
        # (2026-09-08 multi-chat) host/port parameterized per instance from config/llama_servers.json;
        # defaults keep the legacy single-instance behavior exactly.
        "--host", host or "0.0.0.0",
        "--port", str(port),
        # (2026-10 fleet) context is PER SLOT; llama.cpp splits --ctx-size across slots.
        "--ctx-size", str(ctx_total),
        "-n", str(LLAMA_SERVER_MAX_NEW_TOKENS),
        "--threads", str(n_threads),
        "--n-gpu-layers", str(n_gpu_layers),
        # ---- KV Cache Optimization ----
        "--cache-type-k", LLAMA_SERVER_CACHE_TYPE_K,
        "--cache-type-v", LLAMA_SERVER_CACHE_TYPE_V,
        "--cache-ram", str(LLAMA_SERVER_CACHE_RAM),
        # ---- Inference Flags ----
        "--flash-attn", LLAMA_SERVER_FLASH_ATTN,
        "--parallel", str(slots),
        # ---- Fit Mode ----
        "--fit", LLAMA_SERVER_FIT,
        # ---- Server-Level Generation Defaults ----
        "--temp", str(LLAMA_SERVER_TEMP),
        "--repeat-penalty", str(LLAMA_SERVER_REPEAT_PENALTY),
        # ---- File Loading Flags ----
    ]
    if slots > 1:
        logger.info(
            "Launcher: %d inference slots, --ctx-size %d total (%d per slot)",
            slots, ctx_total, ctx_per_slot,
        )

    # Boolean flags (added only when True)
    # NOTE b10441+: --no-mmap is DEPRECATED in favor of --load-mode.
    # Map the config flag to the new equivalent so we never emit a deprecated arg.
    if LLAMA_SERVER_NO_MMAP:
        cmd.extend(["--load-mode", "none"])   # == old --no-mmap (b10441+)
    if LLAMA_SERVER_NO_WARMUP:
        cmd.append("--no-warmup")             # still valid in b10441+
    if LLAMA_SERVER_JINJA:
        cmd.append("--jinja")                 # still valid in b10441+

    # ---- Reasoning / Thinking Mode (Qwen3.6) ----
    if LLAMA_SERVER_REASONING:
        cmd.extend(["--reasoning", "on"])
        cmd.extend(["--reasoning-budget", str(LLAMA_SERVER_REASONING_BUDGET)])
        if LLAMA_SERVER_PRESERVE_THINKING:
            cmd.extend(["--chat-template-kwargs", '{"preserve_thinking":true}'])
        logger.info(
            f"Reasoning mode ENABLED: "
            f"budget={LLAMA_SERVER_REASONING_BUDGET}, "
            f"preserve_thinking={LLAMA_SERVER_PRESERVE_THINKING}"
        )

    # ---- MTP (Multi-Token Prediction) Support ----
    # Auto-detect MTP models by filename and enable speculative decoding
    # llama.cpp b10441 (latest 2026) supports --spec-type draft-mtp
    # See: https://github.com/ggml-org/llama.cpp/pull/22673
    if enable_mtp and LLAMA_SERVER_MTP_ENABLED:
        cmd.extend([
            "--spec-type", LLAMA_SERVER_SPEC_TYPE,
            "--spec-draft-n-max", str(LLAMA_SERVER_SPEC_DRAFT_N_MAX),
            "--spec-draft-n-min", str(LLAMA_SERVER_SPEC_DRAFT_N_MIN),
            "--spec-draft-p-split", str(LLAMA_SERVER_SPEC_DRAFT_P_SPLIT),
            "--spec-draft-p-min", str(LLAMA_SERVER_SPEC_DRAFT_P_MIN),
        ])
        logger.info(
            f"MTP speculative decoding ENABLED: "
            f"type={LLAMA_SERVER_SPEC_TYPE}, "
            f"draft-n-max={LLAMA_SERVER_SPEC_DRAFT_N_MAX}"
        )
    elif enable_mtp and not LLAMA_SERVER_MTP_ENABLED:
        logger.info(
            f"MTP model detected but MTP disabled in config "
            f"(LLAMA_SERVER_MTP_ENABLED=False)"
        )
    else:
        logger.info("Standard inference mode (no MTP speculative decoding)")

    if mmproj_file:
        cmd.extend(["--mmproj", mmproj_file])
        logger.info(f"Loading vision projector: {mmproj_file}")
    else:
        cmd.append("--mmproj-auto")
        logger.info("Using auto-detection for vision projector (--mmproj-auto)")

    if extra_args:
        cmd.extend(str(a) for a in extra_args)

    return cmd




def expected_flag_signatures(model_file: str, enable_mtp: bool,
                             host: str = "0.0.0.0",
                             port: int = LLAMA_SERVER_PORT,
                             parallel: int = LLAMA_SERVER_PARALLEL) -> list[str]:
    """Return the flag substrings OUR OWN launch command would contain.

    start_server uses this to check that a FOREIGN llama-server already bound to our port
    was launched with equivalent flags before reusing it. Instead of duplicating literals,
    the real command is built via _build_server_cmd and the identifying parts extracted:
      --ctx-size <N>   total context window (per-slot budget x slots)
      --parallel <N>   inference slots
      --cache-ram <N>  KV-cache RAM limit
      --flash-attn <v> flash attention setting
      --spec-type draft-mtp (+ companions) only for MTP models

    A foreign command containing ALL of them counts as equivalent to ours; missing any
    makes start_server() restart from zero so the model gets exactly our flags. The model
    path is deliberately NOT a signature - routing owns that decision. Args mirror
    _build_server_cmd() so the signatures always match what we would spawn.
    """
    slots = max(1, int(parallel or 1))
    cmd = _build_server_cmd(
        "llama-server.exe",  # placeholder exe path - never part of any signature
        model_file, None, enable_mtp=enable_mtp, host=host, port=port, parallel=slots,
    )
    sigs: list[str] = [
        f"--ctx-size {CONTEXT_WINDOW_TOKENS * slots}",
        f"--parallel {slots}",
        f"--cache-ram {LLAMA_SERVER_CACHE_RAM}",
        f"--flash-attn {LLAMA_SERVER_FLASH_ATTN}",
    ]
    if enable_mtp and LLAMA_SERVER_MTP_ENABLED:
        sigs.append(f"--spec-type {LLAMA_SERVER_SPEC_TYPE}")
        # the companion flags travel with --spec-type in _build_server_cmd(); checking
        # them too makes a half-configured foreign server (spec type but wrong n-max)
        # fail verification and get restarted.
        sigs.extend([
            f"--spec-draft-n-max {LLAMA_SERVER_SPEC_DRAFT_N_MAX}",
            f"--spec-draft-p-split {LLAMA_SERVER_SPEC_DRAFT_P_SPLIT}",
        ])
    # The list above is hand-written next to the command it must mirror, so it CAN
    # drift when a flag changes in _build_server_cmd(). Checked against the real
    # command instead of trusted: a signature that our own launcher would not emit
    # would silently accept a foreign server we should have restarted.
    joined = " ".join(cmd)
    for sig in sigs:
        if sig not in joined:
            logger.warning(
                "expected_flag_signatures: '%s' is not emitted by _build_server_cmd() - "
                "the signature list drifted from the real launch command", sig,
            )
    return sigs
