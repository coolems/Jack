"""Server management for llama.cpp provider.

Handles starting llama-server, finding model files, and vision projector setup.
OPTIMIZED FOR Qwen3.6 - Based on research from aminr.com and llama.cpp best practices:
- KV cache quantization (--cache-type-k q8_0, --cache-type-v q8_0) halves VRAM for context
- Flash attention (--flash-attn on) cuts VRAM by ~30% and improves throughput
- Cache RAM limit (--cache-ram) prevents OOM on parallel tool calls
- Reasoning mode for Qwen3.6 thinking models
- MTP speculative decoding for >2x speedup

ALL PARAMETERS ARE CENTRALIZED IN config.py - modify there, not here.

This module is the orchestrator. Implementation details are split into:
  - path_utils.py       : Server root & model path resolution
  - command_builder.py  : llama-server CLI argument construction
  - health_check.py     : HTTP health checks & model verification
  - process_mgmt.py     : Subprocess lifecycle (start/stop/kill) + foreign-process inspection
  - model_discovery.py  : .gguf & mmproj file discovery on disk
  - memory.py           : RAM/VRAM budget gate (refuses a launch that would not fit)

(2026-11 multi-config fleet) start_server() takes ONE config.LlamaServerInstance from
config/llama_servers.json instead of loose host/port/concurrency arguments: every entry
carries its own model, slots, ctx/slot, threads, gpu layers, bind and extra flags, so three
entries = three llama-services on three ports on this machine. Two rules define the path:

  * MEMORY FIRST. Before any Popen, memory.check_launch() asks "is there room for this
    model on this machine right now?". If Strata (or another llama node) already ate the
    RAM/VRAM, we REPORT and stop - we never load a second model and go out of memory.
  * ATTACH BY MODEL, NOT BY FLAGS. A server already answering /health on our port is
    reused when its /props reports the model this entry asks for (routing owns the rest -
    slots and ctx are discovered from /props, not demanded). A server running a DIFFERENT
    model is stopped and relaunched with this entry's settings; a non-llama process on the
    port is never killed. The old flag-signature comparison is gone: with per-entry configs,
    "does it carry my flags" is meaningless - what matters is "does it serve my model".

# (2026-09-09 switch-timeout fix): the post-/health model-load budget is config.LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC.
"""

import os
import subprocess
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

from .command_builder import _build_server_cmd

from .health_check import (
    _wait_for_health,
    _verify_model_loaded,
    get_current_model,
)

from .process_mgmt import (
    _log_process_error,
    _terminate_process,
    _kill_orphaned_process,
    _kill_llama_server_by_port,
    wait_for_port_free_async,
    _set_server_process,
    _get_server_process,
    _start_pipe_drainers,
    _pids_listening_on_port,
    _get_process_commandline,
)

from .model_discovery import (
    _find_model_file,
    _find_mmproj_file,
)

from .model_utils import _is_mtp_model
from .model_identity import model_key

# 2026-08-18: authoritative in-memory "which model is loaded" cache.
# We set it here because server.py KNOWS exactly which file it launched —
# no HTTP round-trip needed to confirm what we just did ourselves.
from . import model_state as _model_state


# ---------------------------------------------------------------------------
# Config imports
# ---------------------------------------------------------------------------

from .http_client import get_async_client

from config import (
    CONTEXT_WINDOW_TOKENS,
    LLAMA_SERVER_PARALLEL,
    LLAMA_SERVER_PORT,
    LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC,
    PROVIDER_STARTUP_TIMEOUT,
    LlamaServerInstance,
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
    # Internal helpers (kept for tests)
    "_resolve_server_root",
    "_get_default_models_dir",
    "_resolve_model_path",
    "_resolve_startup_model",
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
    """Resolve which model file to load when an entry names no model (model=None).

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
        logger.warning(
            "Persisted startup model '%s' (%s) not found in any allowed folder - "
            "falling back to first model available",
            persisted_name, persisted_path or "path unknown",
        )

    candidates = _collect_all_gguf(_get_model_scan_dirs(), _get_default_models_dir())
    if not candidates:
        logger.error("No .gguf models found in any allowed folder or default dir")
        return None

    logger.info(
        "Starting with first available model (no valid persisted entry): %s",
        os.path.basename(candidates[0]),
    )
    return candidates[0]


def _resolve_entry_model(instance: LlamaServerInstance) -> str:
    """Resolve the .gguf this entry must run. STRICT — never silently another model.

    entry.model = None  -> whatever this machine persists/has (_resolve_startup_model()).
    entry.model set     -> absolute path that exists, or a name resolved by path_utils;
                           the canonical key MUST match the requested name, otherwise a
                           fallback ("first available model") would launch a DIFFERENT
                           model than the entry says and lie about it in the fleet table.
    Raises RuntimeError with an actionable message when nothing matches.
    """
    raw = (instance.model or "").strip()
    if not raw:
        path = _resolve_startup_model()
        if not path:
            raise RuntimeError(
                "No .gguf model available on this machine - put one in llama_server/models "
                "(or config.LLAMA_MODEL_DIRS), or set 'model' in config/llama_servers.json."
            )
        return path

    if os.path.isabs(raw) and os.path.exists(raw):
        return raw

    resolved = _resolve_model_path(raw)
    if resolved and model_key(resolved) == model_key(raw):
        return resolved

    # Last resort: the persisted model, but only if it IS the requested one.
    fallback = _resolve_startup_model()
    if fallback and model_key(fallback) == model_key(raw):
        return fallback

    from .path_utils import _get_model_scan_dirs
    raise RuntimeError(
        f"model '{raw}' not found. Scanned: {', '.join(_get_model_scan_dirs())}. "
        "Put the .gguf there or set an absolute path in config/llama_servers.json."
    )


def _instance_from_api_url(api_url: str) -> LlamaServerInstance:
    """Build the default local entry for a bare api_url (legacy call sites)."""
    host, _, port = api_url.rsplit(":", 1) if ":" in api_url else (api_url, "", "")
    try:
        port = int(port)
    except ValueError:
        port = LLAMA_SERVER_PORT
    host = host.replace("http://", "").replace("https://", "") or "127.0.0.1"
    return LlamaServerInstance("local", host, port, concurrency=LLAMA_SERVER_PARALLEL)


async def _fetch_props(api_url: str) -> Optional[dict]:
    """One /props read (no auth headers - llama.cpp needs none). None on any failure."""
    from .http_client import get_async_client
    try:
        client = await get_async_client()
        resp = await client.get(f"{api_url}/props", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


async def _spawn_and_verify(api_url: str, cmd: list, model_file: str,
                            inst_key: Optional[Tuple[str, int]]) -> bool:
    """Spawn llama-server with *cmd*, wait for health + verified model load.

    Popen with pipe drainers, /health within PROVIDER_STARTUP_TIMEOUT, then the
    budget-derived model-load verification loop. On success: process registered,
    model-state cache updated, .last_model_loaded.json persisted. On failure: process
    terminated, state invalidated, False returned.

    NOTE (2026-10 fleet): nothing reloads a model here. Each entry keeps the model it was
    launched with; routing picks the model group a request names."""
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
        # /v1/models before declaring success.
        _startup_ok = False
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
            _model_state.mark_loaded(os.path.basename(model_file), key=inst_key)
            model_persistence.write_last_model(_last_loaded_model, _last_loaded_model_full_path)
            logger.info(f"llama.cpp server started successfully with {_last_loaded_model}")
            return True

        logger.warning("llama.cpp server did not become available within expected time.")
        await asyncio.to_thread(_log_process_error, process)
        await asyncio.to_thread(_terminate_process, process)
        _model_state.invalidate(key=inst_key)
        return False

    except Exception as e:
        logger.error(f"Failed to start llama-server: {e}")
        if "process" in dir():
            await asyncio.to_thread(_terminate_process, process)
        return False


async def _port_owner_is_llama(port: int) -> Tuple[Optional[int], Optional[str]]:
    """(pid, cmdline) of the process listening on *port*; cmdline None = uninspectable.

    Used before any kill: a NON-llama process on our port must never be terminated.
    """
    pids = await asyncio.to_thread(_pids_listening_on_port, port)
    if not pids:
        return None, None
    pid = pids[0]
    cmd = await asyncio.to_thread(_get_process_commandline, pid)
    return pid, cmd


async def _attach_if_serves_model(api_url: str, model_file: str, port: int,
                                  inst_key: Optional[Tuple[str, int]]) -> bool:
    """Reuse a server already answering /health on our port — decided by ITS MODEL.

    True  -> /props reports the model this entry asks for. Slots/ctx are whatever the node
             runs; the registry discovers them from /props and clamps routing, so a
             mismatch there is a warning, not a reason to restart.
    False -> different model (caller stops it and relaunches with this entry's settings),
             or /props unavailable (a llama still LOADING or a build without /props - we
             never kill something we cannot identify; the health poller retries).
    """
    props = await _fetch_props(api_url)
    if props:
        reported = str(props.get("model_path") or props.get("model_alias") or "")
        if reported and model_key(reported) == model_key(model_file):
            slots = props.get("total_slots")
            ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
            logger.info(
                "[ATTACH] %s already serves '%s' (slots=%s, ctx/slot=%s) - reusing it; "
                "routing uses what it reports.", api_url, os.path.basename(reported),
                slots if isinstance(slots, int) else "?", ctx if isinstance(ctx, int) else "?",
            )
            _model_state.mark_loaded(os.path.basename(reported), key=inst_key)
            return True
        logger.warning(
            "[ATTACH] %s serves '%s' but this entry asks for '%s' - it will be stopped and "
            "relaunched with this entry's config.", api_url,
            os.path.basename(reported) or "(no model reported)", os.path.basename(model_file),
        )
        return False

    # No /props: identify the port owner before deciding anything.
    pid, cmdline = await _port_owner_is_llama(port)
    if pid and cmdline and "llama" in cmdline.lower():
        logger.warning(
            "[ATTACH] llama-server (PID %d) on port %d has no /props yet - it is probably "
            "still LOADING. Not spawning a second one and not killing it; the health poller "
            "will attach once /props answers.", pid, port,
        )
        return False
    if pid:
        logger.warning(
            "[ATTACH] port %d is owned by PID %d (%s) - not a llama-server we can identify; "
            "it will NOT be killed.", port, pid, (cmdline or "command line unavailable")[:80],
        )
        return False
    logger.warning("[ATTACH] /health answered on %s but nothing listens on port %d - cannot attach.",
                   api_url, port)
    return False


async def start_server(api_url: str, instance: Optional[LlamaServerInstance] = None) -> bool:
    """Bring up (or attach to) ONE llama-server described by *instance*.

    (2026-09 threadless refactor): async. The /health probe and the startup wait loop
    run on the shared httpx pool with await asyncio.sleep(); Popen is non-blocking; pipe
    drainers stay daemon threads (documented Phase C leaf).

    (2026-11 multi-config fleet) *instance* is one entry of config/llama_servers.json and
    owns everything: model, slots (--parallel), ctx/slot, threads, gpu layers, bind,
    extra_args, auto_start. None = the default local entry built from *api_url* (legacy
    call sites keep working).

    Attach-only instances (remote host, kind="strata") are NEVER spawned or killed here -
    a healthy one is used, an unhealthy one reports False and the registry marks it down.

    LOCAL path, in order:
      1. we own a live tracked Popen            -> reuse (our flags are known-good)
      2. something answers /health              -> attach iff it serves THIS entry's model
      3. memory gate (memory.check_launch)      -> refuse + report instead of loading anyway
      4. spawn with this entry's flags + verify

    Step 3 is the out-of-memory guard: an admin who already loaded Strata (or another llama
    node) and starts a second llama gets a concrete "needs ~X MB, only Y MB free" error and
    NO process is spawned.
    """
    global _last_loaded_model, _last_loaded_model_full_path

    inst = instance or _instance_from_api_url(api_url)
    if not inst.is_spawnable:
        # Remote llama nodes and Strata endpoints: probe only.
        try:
            client = await get_async_client()
            resp = await client.get(f"{api_url}/health", timeout=5.0)
            if resp.status_code == 200:
                logger.info("%s endpoint %s is healthy - attaching (attach-only).",
                            inst.kind.upper(), inst.api_url)
                return True
        except Exception:
            pass
        logger.warning(
            "[SERVER] %s instance %s (%s:%d) is NOT reachable - nothing to spawn here; "
            "the health poller retries and routing skips it while down.",
            inst.kind, inst.name, inst.host, inst.port,
        )
        return False

    from .model_state import key_for_api_url as _key_fn
    _inst_key = _key_fn(api_url) or (inst.host.lower(), inst.port)
    slots = max(1, int(inst.concurrency or LLAMA_SERVER_PARALLEL))
    port = inst.port

    try:
        model_file = _resolve_entry_model(inst)
    except RuntimeError as e:
        logger.error("[SERVER] %s: %s", inst.label if hasattr(inst, "label") else inst.name, e)
        return False

    # ---- Already up? ----
    _already_up = False
    try:
        client = await get_async_client()
        resp = await client.get(f"{api_url}/health", timeout=5.0)
        if resp.status_code == 200:
            _already_up = True
    except Exception:
        logger.debug("Health check failed (server not yet running or unavailable)")

    if _already_up:
        tracked = _get_server_process(_inst_key)
        if tracked is not None and tracked.poll() is None:
            logger.info(
                "[ATTACH] llama-server we launched (PID %d) is still running - reusing it.",
                tracked.pid,
            )
            return True
        if await _attach_if_serves_model(api_url, model_file, port, _inst_key):
            logger.info("llama.cpp is already running (serves this entry's model).")
            return True
        # Not ours / wrong model -> free the port below, then respawn with our config.

    await asyncio.to_thread(_kill_orphaned_process, _inst_key)
    if _already_up:
        logger.info("[ATTACH] Freeing port %d before relaunching '%s'...", port, inst.name)
        await asyncio.to_thread(_kill_llama_server_by_port, port)
        if not await wait_for_port_free_async(port):
            logger.error(f"[ATTACH] Port {port} still occupied after kill - cannot relaunch")
            return False

    # ---- MEMORY GATE: never load a model this machine has no room for. ----
    from . import memory
    is_mtp = _is_mtp_model(model_file)
    ok, budget = memory.check_launch(
        model_file, slots=slots,
        ctx_per_slot=inst.ctx_per_slot or CONTEXT_WINDOW_TOKENS,
        gpu_layers=inst.gpu_layers, mtp=is_mtp,
    )
    if not ok:
        logger.error(
            "[MEM-GATE] REFUSED '%s' (%s:%d): %s - NOT spawning. Free memory (stop Strata or "
            "another llama node) or lower ctx_per_slot / slots / quant in config/llama_servers.json.",
            inst.name, inst.host, port, budget,
        )
        return False
    logger.info("[MEM-GATE] '%s' fits: %s", inst.name, budget)

    root_dir = _resolve_server_root()
    llama_server_exe = os.path.join(root_dir, "llama_server", "llama-server.exe")
    if not os.path.exists(llama_server_exe):
        logger.error(f"llama-server.exe not found at {llama_server_exe}")
        return False

    mmproj_file = _find_mmproj_file(model_file)

    # MTP detection: filename heuristic, then the GGUF header (baked-in heads show as
    # '<arch>.nextn_predict_layers' > 0 or 'blk.N.nextn.*' tensors).
    if is_mtp:
        logger.info("MTP detection for %s: ENABLED - speculative decoding flags passed.",
                    os.path.basename(model_file))
    else:
        logger.info("MTP detection for %s: DISABLED - standard inference mode.",
                    os.path.basename(model_file))

    bind = inst.bind or "127.0.0.1"
    cmd = _build_server_cmd(
        llama_server_exe, model_file, mmproj_file, enable_mtp=is_mtp,
        host=bind if bind.lower() not in ("localhost", "127.0.0.1") else "0.0.0.0",
        port=port, parallel=slots,
        ctx_per_slot=inst.ctx_per_slot, threads=inst.threads,
        gpu_layers=inst.gpu_layers, extra_args=list(inst.extra_args),
    )

    return await _spawn_and_verify(api_url, cmd, model_file, _inst_key)
