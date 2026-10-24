"""Server management for llama.cpp provider.

Handles starting llama-server, finding model files, and vision projector setup.
OPTIMIZED FOR QWEN3.6 - Based on research from aminr.com and llama.cpp best practices:
- KV cache quantization (--cache-type-k q8_0, --cache-type-v q8_0) halves VRAM for context
- Flash attention (--flash-attn on) cuts VRAM by ~30% and improves throughput
- Single parallel slot (--parallel 1) for stability
- Cache RAM limit (--cache-ram 2048) prevents OOM on parallel tool calls
- Reasoning mode for Qwen3.6 thinking models
- MTP speculative decoding for >2x speedup

ALL PARAMETERS ARE CENTRALIZED IN config.py - modify there, not here.

This module is the orchestrator. Implementation details are split into:
  - path_utils.py       : Server root & model path resolution
  - command_builder.py  : llama-server CLI argument construction
  - health_check.py     : HTTP health checks & model verification
  - process_mgmt.py     : Subprocess lifecycle (start/stop/kill) + foreign-process inspection
  - model_discovery.py  : .gguf & mmproj file discovery on disk

(2026-10-31 VERIFIED ATTACH): start_server() no longer blindly reuses whatever answers
/health on our port. A FOREIGN llama-server (started outside us, e.g. manually) is only
accepted when its command line carries ALL the flags we would have used ourselves
(expected_flag_signatures: ctx-size / cache-ram / flash-attn + MTP spec-decode for MTP
models). Anything else - or anything uninspectable - is killed and relaunched from zero,
so an MTP model ALWAYS gets its --spec-type draft-mtp flags no matter how the port got
occupied. The old blind shortcut ("llama.cpp is already running.") silently ran models
WITHOUT MTP whenever a manually-started server owned the port first.

# (2026-09-09 switch-timeout fix): the post-/health model-load budget is config.LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC.
"""

import os
import subprocess
import time as _time
import logging
import asyncio

from typing import Optional, Tuple

logger = logging.getLogger("COOLEMS.Provider.Llama.Server")


# ---------------------------------------------------------------------------
# Module-level state (shared with process_mgmt helpers)
# ---------------------------------------------------------------------------

_last_loaded_model: Optional[str] = None  # Track last successfully loaded model filename
_last_loaded_model_full_path: Optional[str] = None  # Track full path of last loaded model


# ---------------------------------------------------------------------------
# Imports from sub-modules (the actual implementation)
# ---------------------------------------------------------------------------

from .path_utils import (
    _resolve_server_root,
    _get_default_models_dir,
    _resolve_model_path,
)

from .command_builder import _build_server_cmd, expected_flag_signatures

from .health_check import (
    _wait_for_health,
    _verify_model_loaded,
    get_current_model,
)

from .process_mgmt import (

    _poll_stderr,
    _check_stderr_early,
    _log_process_error,
    _terminate_process,
    _kill_orphaned_process,
    _kill_llama_server_by_port,
    wait_for_port_free_async,
    _set_server_process,
    _get_server_process,
    _start_pipe_drainers,
    _find_pid_on_port,
    _get_process_commandline,
)

from .model_discovery import (
    _find_model_file,
    _find_mmproj_file,
)

# 2026-10-31: MTP detection moved to top-level import - start_server() now runs it in the
# verified-attach decision too (not only at spawn time).
from .model_utils import _is_mtp_model

# 2026-08-18: authoritative in-memory "which model is loaded" cache.
# We set it here because server.py KNOWS exactly which file it launched —
# no HTTP round-trip needed to confirm what we just did ourselves.
from . import model_state as _model_state


# ---------------------------------------------------------------------------
# Config imports
# ---------------------------------------------------------------------------

from .http_client import get_async_client

from config import (
    HEALTH_CHECK_TIMEOUT,
    PROVIDER_STARTUP_TIMEOUT,
    LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC,
    LLAMA_SERVER_PORT,
    LLAMA_SERVER_PARALLEL,
)


# 2026-08-23: .last_model_loaded.json persistence — SOURCE OF TRUTH for the model
# to load on startup. Written after every verified launch/switch (see below).
from . import model_persistence

# ---------------------------------------------------------------------------
# Re-export everything so existing `from .server import ...` still works
# ---------------------------------------------------------------------------

__all__ = [
    # Public API
    "start_server",
    "get_current_model",
    "expected_flag_signatures",
    # Internal helpers (kept for backward compat with tests)
    "_resolve_server_root",
    "_get_default_models_dir",
    "_resolve_model_path",
    "_build_server_cmd",
    "_wait_for_health",
    "_verify_model_loaded",
    "_terminate_process",
    "_kill_orphaned_process",
    "_kill_llama_server_by_port",
    "_find_model_file",
    "_find_mmproj_file",
]


# ---------------------------------------------------------------------------
# PUBLIC API - start_server
# ---------------------------------------------------------------------------

def _resolve_startup_model() -> Optional[str]:
    """Resolve which model file to load on startup (2026-08-23).

    Resolution chain — .last_model_loaded.json is the SOURCE OF TRUTH:
      1. Persisted model name from config/.last_model_loaded.json, resolved
         through the local model scan dirs (strict matching in path_utils).
      2. If nothing persisted / not resolvable -> FIRST model available on disk
         (deterministic scan order), so a fresh machine still boots with something
         valid.

    Returns:
        Full path to the .gguf file, or None when no model exists at all.
    """
    from .path_utils import _collect_all_gguf, _get_model_scan_dirs, _get_default_models_dir

    persisted_name, persisted_path = model_persistence.read_last_model()

    if persisted_name:
        resolved = _resolve_model_path(persisted_name)
        if resolved:
            logger.info(
                "Startup model from .last_model_loaded.json (source of truth): %s",
                os.path.basename(resolved),
            )
            return resolved
        # Persisted model is not available in the active profile's folders.
        logger.warning(
            "Persisted startup model '%s' (%s) not found in any allowed profile folder - "
            "falling back to first model available in profile",
            persisted_name, persisted_path or "path unknown",
        )

    # Fallback: first model available across the profile's allowed folders.
    candidates = _collect_all_gguf(_get_model_scan_dirs(), _get_default_models_dir())
    if not candidates:
        logger.error("No .gguf models found in any allowed folder or default dir")
        return None

    logger.info(
        "Starting with first available profile model (no valid persisted entry): %s",
        os.path.basename(candidates[0]),
    )
    return candidates[0]


async def _spawn_and_verify(api_url: str, cmd: list, model_file: str,
                            inst_key: Optional[Tuple[str, int]]) -> bool:
    """Spawn llama-server with *cmd*, wait for health + verified model load.

    (2026-10-31) Shared by start_server(): Popen with pipe
    drainers, /health within PROVIDER_STARTUP_TIMEOUT, then the budget-derived
    model-load verification loop. On success: process registered, model-state cache
    updated, .last_model_loaded.json persisted. On failure: process terminated,
    state invalidated, False returned.

    NOTE (2026-10 fleet): the old model-switch/reload path was removed; nodes keep the
    model they were launched with and routing picks the matching model group."""
    global _last_loaded_model, _last_loaded_model_full_path

    try:
        logger.info(f"Starting llama-server with model: {model_file}")
        logger.info(f"Command: {' '.join(cmd)}")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=os.path.dirname(cmd[0]),
        )
        # CRITICAL: drain stdout/stderr forever. Without this the OS pipe buffer
        # fills up and llama.cpp blocks on write() -> GPU idle + ALL requests hang.
        _start_pipe_drainers(process)

        # (2026-09-09 fresh-start fix) /health alone is NOT proof the model loaded:
        # this build of llama-server can answer 200 while running in router mode with
        # ZERO models loaded. Poll until the expected model actually shows up in
        # /v1/models (the same check the old reload loop used) before declaring success -
        # a cold load of a ~22 GB model legitimately takes minutes after /health comes up.
        _startup_ok = False
        # (2026-09-09 switch-timeout fix) budget-derived attempt count: a COLD load of a
        # ~23 GB model can legitimately need several minutes after /health comes up.
        _load_attempts = max(1, LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC // 2)
        if await _wait_for_health(api_url, PROVIDER_STARTUP_TIMEOUT):
            for _attempt in range(_load_attempts):
                if await _verify_model_loaded(api_url, model_file):
                    _startup_ok = True
                    break
                logger.info(f"[STARTUP] Server healthy but model not loaded yet (attempt {_attempt + 1}/{_load_attempts})")
                await asyncio.sleep(2)

        if _startup_ok:
            _set_server_process(process, key=inst_key)
            _last_loaded_model = os.path.basename(model_file)
            _last_loaded_model_full_path = model_file
            # 2026-08-18: publish to the shared model-state cache so chat requests
            # (2026-09-08 multi-chat) keyed per instance.
            _model_state.mark_loaded(os.path.basename(model_file), key=inst_key)
            # 2026-08-23: persist as source of truth for the NEXT startup.
            model_persistence.write_last_model(_last_loaded_model, _last_loaded_model_full_path)
            logger.info(f"llama.cpp server started successfully with {_last_loaded_model}")
            return True

        # Health check loop failed - kill the orphaned process and log stderr
        logger.warning("llama.cpp server did not become available within expected time.")
        await asyncio.to_thread(_log_process_error, process)
        await asyncio.to_thread(_terminate_process, process)
        _model_state.invalidate(key=inst_key)  # we killed/failed a launch - this instance's model unknown again
        return False

    except Exception as e:
        logger.error(f"Failed to start llama-server: {e}")
        if "process" in dir():
            await asyncio.to_thread(_terminate_process, process)
        return False


async def _attach_foreign_if_verified(api_url: str, model_file: str,
                                      host: str, port: int,
                                      inst_key: Optional[Tuple[str, int]],
                                      parallel: int = 1) -> bool:
    """(2026-10-31 verified attach) Decide whether a FOREIGN server on our port is good enough.

    Called only when /health answers 200 but we do NOT own the process (no tracked Popen).
    Returns True ONLY when the foreign command line carries every flag signature we would
    have used ourselves for *model_file* - in that case it is equivalent to a server we
    launched, so reusing it costs nothing. Any missing signature (most importantly: an MTP
    model running without --spec-type draft-mtp) or an uninspectable command line means
    False -> the caller kills and relaunches from zero with our full flags.

    The decision is always logged explicitly so "is MTP on?" is answerable from coolems.log."""
    _eff_port = port or LLAMA_SERVER_PORT
    _bind_host = host or "127.0.0.1"

    # 1) Which model would WE load, and does it have a baked-in MTP head?
    is_mtp = _is_mtp_model(model_file)
    sigs = expected_flag_signatures(
        model_file, enable_mtp=is_mtp,
        host=_bind_host if _bind_host != "localhost" else "0.0.0.0",
        port=_eff_port,
        parallel=parallel,
    )

    # 2) Who owns the port? (blocking netstat leaf -> to_thread)
    pid = await asyncio.to_thread(_find_pid_on_port, _eff_port)
    if not pid:
        logger.warning(
            "[ATTACH] /health answered on %s:%d but no LISTENING PID found - cannot verify; "
            "restarting from zero.", _bind_host, _eff_port,
        )
        return False

    # 3) What was it launched with? (blocking wmic/powershell leaf -> to_thread)
    cmdline = await asyncio.to_thread(_get_process_commandline, pid)
    if not cmdline:
        logger.warning(
            "[ATTACH] Foreign llama-server PID %d on port %d is UNINSPECTABLE "
            "(wmic/PowerShell unavailable or access denied). Safe path: kill and relaunch "
            "from zero so the model gets our exact flags (MTP=%s).",
            pid, _eff_port, "ENABLED" if is_mtp else "not needed",
        )
        return False

    missing = [sig for sig in sigs if sig not in cmdline]
    if not missing:
        logger.info(
            "[ATTACH] Foreign llama-server PID %d VERIFIED: command line carries all our "
            "expected flags (MTP=%s) - reusing it instead of restarting.",
            pid, "ENABLED" if is_mtp else "not needed",
        )
        # Record the foreign model as loaded so the auto-switch check has a baseline.
        try:
            cur = await get_current_model(api_url)
            if cur:
                _model_state.mark_loaded(cur, key=inst_key)
        except Exception as e:
            logger.debug(f"[ATTACH] Could not read foreign model id (non-fatal): {e}")
        return True

    logger.warning(
        "[ATTACH] Foreign llama-server PID %d on port %d is MISSING our flags: %s. "
        "Killing it and relaunching from zero with the full command (MTP=%s).",
        pid, _eff_port, missing, "ENABLED" if is_mtp else "not needed",
    )
    return False


async def start_server(api_url: str, host: str = None, port: int = None,
                       concurrency: int = 1) -> bool:
    """Start llama.cpp server if not already running.

    (2026-09 threadless refactor): async. The /health probe and the startup wait loop
    run on the shared httpx pool with await asyncio.sleep(); Popen is non-blocking; pipe
    drainers stay daemon threads (documented Phase C leaf - blocking pipe reads have no

    (2026-09-08 multi-chat) *host*/*port* parameterize the instance (from config/llama_servers.json);
    they default to the legacy local single instance. Remote instances (non-loopback host) are
    ATTACH-ONLY: we never spawn processes on other machines - a healthy remote is simply used,
    an unhealthy one reports False and the registry marks it unavailable.

    (2026-10-31 verified attach): "already running" is no longer enough for LOCAL instances.
    A foreign server on our port must carry all of OUR expected flags (see
    _attach_foreign_if_verified) or it is killed and relaunched from zero - this guarantees
    an MTP model always boots with speculative decoding, even when a manually-started
    llama-server owned the port first.

    (2026-10 fleet) *concurrency* is this LOCAL instance's slot count: we pass
    --parallel N and scale --ctx-size to N x CONTEXT_WINDOW_TOKENS so every slot keeps
    the full context. Remote instances ignore it (attach-only; slots come from /props)."""
    global _last_loaded_model, _last_loaded_model_full_path

    # (2026-09-08 multi-chat) per-instance key for the model-state cache + process registry.
    from .model_state import key_for_api_url as _key_fn
    _inst_key = _key_fn(api_url) or ((host or "127.0.0.1").lower(), port or LLAMA_SERVER_PORT)
    slots = max(1, int(concurrency or 1))

    # Remote instances are attach-only: never spawn processes on other machines. A healthy
    # remote is simply used (we cannot - and must not - inspect/kill its process); an
    # unhealthy one reports False and the registry marks it unavailable.
    _bind_host = host or "127.0.0.1"
    if _bind_host.lower() not in ("127.0.0.1", "localhost", "::1"):
        try:
            client = await get_async_client()
            resp = await client.get(f"{api_url}/health")
            if resp.status_code == 200:
                logger.info("Remote llama-server %s is healthy - attaching (attach-only).", _bind_host)
                return True
        except Exception:
            pass
        logger.warning(
            "[SERVER] Remote llama-server instance %s:%d is NOT reachable - nothing to spawn here; "
            "it will be retried by the health poller and simply not picked while unhealthy.",
            _bind_host, port or LLAMA_SERVER_PORT,
        )
        return False

    # Which model would we load? (needed for BOTH the attach decision and a fresh spawn)
    model_file = _resolve_startup_model()
    if not model_file:
        logger.error("No suitable model file found")
        return False

    # First check if already running (async pooled probe). LOCAL instances only - remote
    # handled above. A 200 here does NOT mean "done": it may be a foreign process started
    # outside us, and that is exactly the case the verified-attach logic below must judge.
    _already_up = False
    try:
        client = await get_async_client()
        resp = await client.get(f"{api_url}/health")
        if resp.status_code == 200:
            _already_up = True
    except Exception:
        logger.debug("Health check failed (server not yet running or unavailable)")

    # ---- Fast path: WE own the process (tracked Popen, still alive) -> trust it. ----
    # We launched it with our full command, so its flags are known-good by construction;
    # no netstat/wmic inspection needed. This is also the rollback-after-failed-switch case
    # and the "server already up from a previous start_server()" case - both stay instant.
    if _already_up:
        tracked = _get_server_process(_inst_key)
        if tracked is not None and tracked.poll() is None:
            logger.info(
                "[ATTACH] llama-server we launched (PID %d) is still running - reusing it "
                "(our flags are known-good by construction).", tracked.pid,
            )
            return True

        # ---- Verified attach: a FOREIGN process answers /health on our port. ----
        if await _attach_foreign_if_verified(
            api_url, model_file, _bind_host, port or LLAMA_SERVER_PORT, _inst_key, parallel=slots,
        ):
            logger.info("llama.cpp is already running (verified foreign server - all expected flags present).")
            return True

        # Foreign server failed verification (or was uninspectable) -> fall through to a
        # full kill + fresh spawn from zero below. The logs above say exactly why.

    # Kill any previously tracked orphaned process for THIS instance before starting new one
    await asyncio.to_thread(_kill_orphaned_process, _inst_key)
    if _already_up:
        # A foreign (untracked) server owns the port - kill it by PID/port so the fresh
        # spawn below can bind. (Tracked processes were already terminated above.)
        logger.info("[ATTACH] Killing unverified foreign server on port %d before relaunch...", port or LLAMA_SERVER_PORT)
        await asyncio.to_thread(_kill_llama_server_by_port, port or LLAMA_SERVER_PORT)
        if not await wait_for_port_free_async(port or LLAMA_SERVER_PORT):
            logger.error(f"[ATTACH] Port {port or LLAMA_SERVER_PORT} still occupied after kill - cannot relaunch")
            return False

    # Find llama-server.exe
    root_dir = _resolve_server_root()
    llama_server_exe = os.path.join(root_dir, "llama_server", "llama-server.exe")

    if not os.path.exists(llama_server_exe):
        logger.error(f"llama-server.exe not found at {llama_server_exe}")
        return False

    # Find the mmproj (vision projector) file - search in model folder too
    mmproj_file = _find_mmproj_file(model_file)

    # Build command with optimized parameters.
    # MTP detection (2026-10-31): first the filename ('MTP' in name, legacy behavior), then
    # INSIDE the .gguf header - baked-in heads are visible as '<arch>.nextn_predict_layers' > 0
    # or 'blk.N.nextn.*' tensors (see model_utils._is_mtp_model). The unsloth/Dirk GGUFs bake
    # the full MTP head INTO the main file, so no separate mtp-*.gguf is downloaded or loaded.
    is_mtp = _is_mtp_model(model_file)
    if is_mtp:
        logger.info(
            "MTP detection for %s: ENABLED (baked-in head detected in GGUF header) - "
            "speculative decoding flags will be passed to llama-server.",
            os.path.basename(model_file),
        )
    else:
        logger.info(
            "MTP detection for %s: DISABLED (no baked-in MTP head) - standard inference mode.",
            os.path.basename(model_file),
        )

    cmd = _build_server_cmd(
        llama_server_exe, model_file, mmproj_file, enable_mtp=is_mtp,
        host=_bind_host if _bind_host.lower() != "localhost" else "0.0.0.0",
        port=port or LLAMA_SERVER_PORT,
        parallel=slots,
    )

    return await _spawn_and_verify(api_url, cmd, model_file, _inst_key)

