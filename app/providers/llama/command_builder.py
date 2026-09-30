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
                      port: int = LLAMA_SERVER_PORT) -> list:
    """Build the llama-server command with optimal parameters for Qwen3.6.

    All parameters come from config.py — modify there, not here.

    Auto-enables MTP speculative decoding when enable_mtp=True and the model
    is an MTP model. MTP provides >2x speedup with ~75% token acceptance rate.

    Args:
        host: Bind address for this instance (default "0.0.0.0" = all interfaces).
        port: TCP port for this instance's HTTP endpoint (default LLAMA_SERVER_PORT).
        llama_server_exe: Path to llama-server.exe
        model_file: Path to the .gguf model file
        mmproj_file: Optional path to mmproj file for vision
        enable_mtp: Whether to enable MTP speculative decoding params

    NOTE (2026-09-19): NO --model-draft is ever emitted. The unsloth Qwen3.8 GGUFs bake the
    full MTP layer + head INTO the main file; with only --spec-type draft-mtp, llama.cpp
    creates a second MTP context on top of the SAME model (no extra weights loaded).

    Returns:
        The command list ready for subprocess.Popen.
    """
    ctx_size = CONTEXT_WINDOW_TOKENS
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
        "--ctx-size", str(ctx_size),
        "-n", str(LLAMA_SERVER_MAX_NEW_TOKENS),
        "--threads", str(LLAMA_SERVER_THREADS),
        "--n-gpu-layers", str(LLAMA_SERVER_GPU_LAYERS),
        # ---- KV Cache Optimization ----
        "--cache-type-k", LLAMA_SERVER_CACHE_TYPE_K,
        "--cache-type-v", LLAMA_SERVER_CACHE_TYPE_V,
        "--cache-ram", str(LLAMA_SERVER_CACHE_RAM),
        # ---- Inference Flags ----
        "--flash-attn", LLAMA_SERVER_FLASH_ATTN,
        "--parallel", str(LLAMA_SERVER_PARALLEL),
        # ---- Fit Mode ----
        "--fit", LLAMA_SERVER_FIT,
        # ---- Server-Level Generation Defaults ----
        "--temp", str(LLAMA_SERVER_TEMP),
        "--repeat-penalty", str(LLAMA_SERVER_REPEAT_PENALTY),
        # ---- File Loading Flags ----
    ]

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

    return cmd
