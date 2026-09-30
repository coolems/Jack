"""Dedicated OCR llama-server lifecycle manager (2026-08-26).

WHY THIS EXISTS
---------------
The main chat server (port 5000) usually runs a big non-vision model (e.g.
Qwen3.8-27B-MTP, no mmproj next to it), so POSTing an image there returns:

    HTTP 500 {"error": "image input is not supported - ... you may need to provide the mmproj"}

The OCR model configured in profiles.json (GLM-OCR + its mmproj) sits on disk
but is never launched by anything — llama-server in single-model mode simply
IGNORES the "model" field of a chat payload and serves whatever it loaded.

This module closes that gap: on demand it launches a SMALL dedicated
llama-server instance for OCR (GLM-OCR + mmproj) on its own side port
(config.LLAMA_OCR_SERVER_PORT, default 5010), waits until healthy, verifies the
right model is loaded, and keeps the process warm so subsequent transcriptions
are instant. It never touches the main chat server — zero disruption to an
active conversation.

Design rules:
  * Profile-driven: model + mmproj come from profiles.json 'ocr_model' dirs
    (same source of truth as app/initialization.py). No hardcoded model names.
  * Idempotent: if a healthy OCR instance with the expected model is already up
    (launched by us or externally), it is reused — no duplicate processes.
  * Safe cleanup: we only ever terminate processes WE launched ourselves.
"""

import asyncio
import glob
import json
import logging
import os
import subprocess
import threading
from typing import List, Optional, Tuple

logger = logging.getLogger("COOLEMS.Provider.Llama.OCRSrv")


# ---------------------------------------------------------------------------
# Module state (tracked process — we only ever kill what WE started)
# ---------------------------------------------------------------------------
_ocr_process: Optional[subprocess.Popen] = None
# PHASE C LEAF (2026-09 threadless refactor): the lock stays because this module's blocking
# helpers (_launch_and_wait, _terminate_process, _is_healthy...) are called from
# asyncio.to_thread() workers - multiple worker threads CAN touch this state at once. The lock
# is only ever held for nanosecond state reads/writes (never across an await or a blocking
# wait); all long work happens outside it on the loop or in to_thread.
_lock = threading.Lock()
_last_model_path: Optional[str] = None


def _port() -> int:
    from config import LLAMA_OCR_SERVER_PORT
    return int(LLAMA_OCR_SERVER_PORT)


def _base_url() -> str:
    return f"http://localhost:{_port()}"


# ---------------------------------------------------------------------------
# Discovery — which OCR model + mmproj should the dedicated instance load?
# ---------------------------------------------------------------------------

def resolve_ocr_candidates() -> List[Tuple[str, str]]:
    """Resolve (model_path, mmproj_path) pairs from profiles.json 'ocr_model' dirs.

    Same source of truth as app/initialization.py._resolve_ocr_models_from_profiles():
    every unique ocr_model directory contributes one candidate when it contains a
    non-mmproj .gguf model file and an mmproj projector next to it.

    Returns: list of (model_path, mmproj_path) — empty when nothing usable exists.
    Never raises.
    """
    from config import PROFILES_FILE

    candidates: List[Tuple[str, str]] = []
    seen_dirs = set()
    try:
        if not os.path.exists(PROFILES_FILE):
            logger.warning(f"[OCRSRV] Profiles file not found at {PROFILES_FILE}")
            return []
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            profiles = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[OCRSRV] Failed to load profiles.json: {e}")
        return []

    for role_name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        ocr_dir = profile.get("ocr_model")
        if not ocr_dir or not isinstance(ocr_dir, str):
            continue
        abs_dir = os.path.abspath(ocr_dir)
        if abs_dir in seen_dirs:
            continue
        seen_dirs.add(abs_dir)

        if not os.path.isdir(abs_dir):
            logger.warning(f"[OCRSRV] OCR model directory not found for role '{role_name}': {abs_dir}")
            continue

        # Model file = first .gguf that is NOT a projector
        model_path: Optional[str] = None
        mmproj_path: Optional[str] = None
        for fpath in sorted(glob.glob(os.path.join(abs_dir, "*.gguf"))):
            fname_lower = os.path.basename(fpath).lower()
            if "mmproj" in fname_lower or "projector" in fname_lower:
                if mmproj_path is None:
                    mmproj_path = fpath
            elif model_path is None:
                model_path = fpath

        if model_path and mmproj_path:
            candidates.append((model_path, mmproj_path))
        else:
            logger.warning(
                f"[OCRSRV] OCR dir {abs_dir} incomplete (model={bool(model_path)}, "
                f"mmproj={bool(mmproj_path)}) — skipped"
            )

    return candidates


# ---------------------------------------------------------------------------
# Health / verification helpers.
# NOTE: _wait_for_health/_verify_model_loaded in .health_check are ASYNC (2026-09 threadless
# refactor) - they must be AWAITED on the loop, never called from a worker thread. The sync
# helpers below (_is_healthy, _loaded_model_id) use short requests calls and run via to_thread.
# ---------------------------------------------------------------------------

def _is_healthy(port: int) -> bool:
    try:
        import requests as _requests
        resp = _requests.get(f"http://localhost:{port}/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def _loaded_model_id(port: int) -> str:
    """Return the model id reported by /v1/models on this port ('' when unknown)."""
    try:
        import requests as _requests
        resp = _requests.get(f"http://localhost:{port}/v1/models", timeout=3)
        data = resp.json().get("data", [])
        if data:
            return str(data[0].get("id", ""))
    except Exception:
        pass
    return ""


def _model_matches(loaded_id: str, expected_path: str) -> bool:
    """EXACT match only (same rule as health_check._verify_model_loaded).

    2026-08-23 security fix: the old bidirectional substring match could let a
    wrong model pass verification; compare stripped basenames for equality.
    """
    if not loaded_id or not expected_path:
        return False
    a = os.path.basename(expected_path).replace(".gguf", "").lower()
    b = loaded_id.replace(".gguf", "").lower()
    return a == b


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def _build_ocr_cmd(llama_server_exe: str, model_file: str, mmproj_file: str) -> list:
    """Command for the dedicated OCR instance — lean flags tuned for a small model.

    Deliberately NO MTP speculative decoding and NO reasoning mode (GLM-OCR is a
    tiny dense vision model). KV-cache quant + flash-attn kept from config to stay
    consistent with the main server's VRAM discipline.
    """
    from config import (
        LLAMA_OCR_CTX_SIZE,
        LLAMA_SERVER_THREADS,
        LLAMA_SERVER_GPU_LAYERS,
        LLAMA_SERVER_FLASH_ATTN,
        LLAMA_SERVER_PARALLEL,
        LLAMA_SERVER_CACHE_TYPE_K,
        LLAMA_SERVER_CACHE_TYPE_V,
    )

    cmd = [
        llama_server_exe,
        "--model", model_file,
        "--mmproj", mmproj_file,          # explicit projector — the whole point of this instance
        "--host", "0.0.0.0",
        "--port", str(_port()),
        "--ctx-size", str(LLAMA_OCR_CTX_SIZE),
        "--threads", str(int(LLAMA_SERVER_THREADS)),
        "--n-gpu-layers", str(int(LLAMA_SERVER_GPU_LAYERS)),
        "--cache-type-k", LLAMA_SERVER_CACHE_TYPE_K,
        "--cache-type-v", LLAMA_SERVER_CACHE_TYPE_V,
        "--flash-attn", LLAMA_SERVER_FLASH_ATTN,
        "--parallel", str(int(LLAMA_SERVER_PARALLEL)),
    ]
    logger.info(f"[OCRSRV] Launch command: {' '.join(cmd)}")
    return cmd


def _popen_ocr(model_file: str, mmproj_file: str) -> Optional[subprocess.Popen]:
    """Sync leaf: build the command and Popen the dedicated OCR instance.

    Runs via asyncio.to_thread() (Popen + pipe-drainer setup). Returns None when
    llama-server.exe is missing."""
    from .path_utils import _resolve_server_root
    from .process_mgmt import _start_pipe_drainers

    root_dir = _resolve_server_root()
    llama_server_exe = os.path.join(root_dir, "llama_server", "llama-server.exe")
    if not os.path.exists(llama_server_exe):
        logger.error(f"[OCRSRV] llama-server.exe not found at {llama_server_exe}")
        return None

    cmd = _build_ocr_cmd(llama_server_exe, model_file, mmproj_file)
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
        cwd=os.path.dirname(llama_server_exe),
    )
    # CRITICAL (same as main server): drain pipes forever or b10441 deadlocks on write.
    _start_pipe_drainers(process)
    return process


async def _launch_and_wait(model_file: str, mmproj_file: str) -> bool:
    """Start the OCR llama-server and wait until healthy + verified (ASYNC).

    (2026-09 threadless refactor): split from the old fully-blocking version. The Popen
    leaf runs in a worker thread; the health wait / model verification are AWAITED on the
    loop directly - .health_check._wait_for_health and _verify_model_loaded are async now,
    so they can no longer be called from a to_thread() worker (that was silently skipping
    them: awaiting nothing)."""
    global _ocr_process

    from .health_check import _wait_for_health, _verify_model_loaded
    from .process_mgmt import _terminate_process, _log_process_error

    process = await asyncio.to_thread(_popen_ocr, model_file, mmproj_file)
    if process is None:
        return False

    max_attempts = 60          # 60 * ~2s sleep ≈ 120 s budget — GLM-OCR loads in seconds
    try:
        healthy = await _wait_for_health(_base_url(), max_attempts, sleep_sec=2)
    except Exception as e:
        logger.error(f"[OCRSRV] Health wait failed unexpectedly: {e}")
        healthy = False
    if not healthy:
        logger.error("[OCRSRV] OCR server did not become healthy within timeout")
        await asyncio.to_thread(_log_process_error, process)
        await asyncio.to_thread(_terminate_process, process)
        return False

    try:
        verified = await _verify_model_loaded(_base_url(), model_file)
    except Exception as e:
        logger.error(f"[OCRSRV] Model verification failed unexpectedly: {e}")
        verified = False
    if not verified:
        logger.error("[OCRSRV] Server healthy but expected OCR model is NOT the loaded one — aborting")
        await asyncio.to_thread(_terminate_process, process)
        return False

    _ocr_process = process
    logger.info(f"[OCRSRV] ===== Dedicated OCR server READY on port {_port()} (PID {process.pid}) =====")
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_launch_in_progress = False
_launch_event: Optional[asyncio.Event] = None


def _tracked_state() -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """Snapshot (tracked_process, last_model_path) - short critical section only."""
    with _lock:
        return _ocr_process, _last_model_path


async def ensure_ocr_server() -> Optional[str]:
    """Make sure a vision-capable dedicated OCR server is running.

    Returns the base URL (e.g. "http://localhost:5010") when one is available,
    or None when it could not be started/verified. Idempotent and safe to call
    on every OCR request - reuse is a cheap /health probe.

    Locking discipline (2026-08-27 fix): the threading lock is only ever held
    for instantaneous state reads/writes - NEVER across an await or a blocking
    wait. All long-running work (health probes, model verification, process
    launch up to ~120 s) runs OUTSIDE the lock via asyncio.to_thread, so two
    concurrent OCR requests can no longer stall each other on lock acquisition.
    A separate _launch_in_progress flag + Event serializes the slow path so
    simultaneous requests never double-start the server and a cleanup can never
    race an external-reuse probe.
    """
    global _ocr_process, _last_model_path, _launch_in_progress, _launch_event

    port = _port()

    # ---- Fast path 1 (read-only snapshot): our tracked process warm & valid? ----
    proc, last_model = _tracked_state()
    if proc is not None and proc.poll() is None:
        loaded = await asyncio.to_thread(_loaded_model_id, port)
        if loaded and (not last_model or _model_matches(loaded, last_model)):
            logger.debug(f"[OCRSRV] Reusing warm OCR server on port {port} ({os.path.basename(loaded)})")
            return _base_url()

    # ---- Slow path: everything below mutates state -> serialize via flag+event ----
    while True:
        with _lock:  # short critical section - no await inside
            if _launch_in_progress and _launch_event is not None:
                wait_ev = _launch_event          # someone else is starting it
            else:
                wait_ev = asyncio.Event()        # I take the launcher role
                _launch_in_progress = True
                _launch_event = wait_ev
                break

        # Waiter branch: block (bounded) until the current launcher finishes.
        logger.info("[OCRSRV] OCR server startup already in progress by another request - waiting")
        try:
            await asyncio.wait_for(wait_ev.wait(), timeout=150)
        except asyncio.TimeoutError:
            logger.warning("[OCRSRV] Timed out waiting for concurrent OCR launch - re-checking state")

        # Re-check fast path 1: the launcher may have left a valid warm server behind.
        proc, last_model = _tracked_state()
        if proc is not None and proc.poll() is None:
            loaded = await asyncio.to_thread(_loaded_model_id, port)
            if loaded and (not last_model or _model_matches(loaded, last_model)):
                logger.debug(f"[OCRSRV] Reusing OCR server started by concurrent request ({os.path.basename(loaded)})")
                return _base_url()

        # Nothing valid yet - loop back to re-acquire the launcher role.

    try:
        # Exclusive actor from here on (I hold the launcher role).

        # ---- Clean up a stale tracked process OUTSIDE the lock ----
        with _lock:  # short snapshot only
            stale = _ocr_process
        if stale is not None and stale.poll() is None:
            # Healthy but wrong model -> terminate before reusing the port.
            logger.warning("[OCRSRV] Tracked OCR process no longer valid - restarting it")
            from .process_mgmt import _terminate_process
            with _lock:  # short state clear for THIS exact object only (no race)
                if _ocr_process is stale:
                    _ocr_process = None
                    _last_model_path = None
            await asyncio.to_thread(_terminate_process, stale)   # blocking wait OUTSIDE lock
        elif stale is not None:
            with _lock:  # already dead - just clear the reference
                if _ocr_process is stale:
                    _ocr_process = None
                    _last_model_path = None

        # ---- Fast path 2: something ELSE already healthy on the port? ----
        # (Safe now: no concurrent cleanup can race this probe.)
        if await asyncio.to_thread(_is_healthy, port):
            loaded = await asyncio.to_thread(_loaded_model_id, port)
            candidates = resolve_ocr_candidates()
            for model_path, _mm in candidates:
                if _model_matches(loaded, model_path):
                    logger.info(f"[OCRSRV] Reusing external OCR server already on port {port} ({os.path.basename(loaded)})")
                    return _base_url()
            logger.warning(
                f"[OCRSRV] Port {port} is occupied by a different model "
                f"({loaded or 'unknown'}) - cannot start the dedicated OCR instance there."
            )
            return None

        # ---- Launch path: resolve candidates from profiles.json and start one ----
        candidates = resolve_ocr_candidates()
        if not candidates:
            logger.error("[OCRSRV] No usable (model + mmproj) pair found in profiles.json 'ocr_model' dirs")
            return None

        for model_path, mmproj_path in candidates:
            with _lock:  # short write - no await inside
                _last_model_path = model_path
            logger.info(
                f"[OCRSRV] Starting dedicated OCR instance: {os.path.basename(model_path)} "
                f"+ {os.path.basename(mmproj_path)} on port {port}"
            )
            ok = await _launch_and_wait(model_path, mmproj_path)  # up to ~120 s, OUTSIDE lock
            if ok:
                return _base_url()

        logger.error("[OCRSRV] All OCR candidates failed to start")
        return None
    finally:
        # Wake waiters FIRST (they re-check state), then release the launcher role.
        wait_ev.set()
        with _lock:  # short critical section - no await inside
            _launch_in_progress = False
            _launch_event = None


def unload_ocr_server() -> bool:
    """Terminate the tracked dedicated OCR process to free its VRAM.

    Called automatically after a SUCCESSFUL transcription when config.LLAMA_OCR_UNLOAD_AFTER_USE
    is True (see ocr.py). Idempotent and safe: if nothing we launched is running it simply returns
    False. We only ever terminate processes WE started - an externally-launched instance on the port
    is left alone.

    Returns True when a process was actually terminated, False when there was nothing to unload.
    """
    global _ocr_process, _last_model_path

    with _lock:  # short critical section - no blocking work inside
        if _ocr_process is None or _ocr_process.poll() is not None:
            logger.debug("[OCRSRV] unload requested but no tracked OCR process is running - nothing to do")
            return False

        from .process_mgmt import _terminate_process
        logger.info(f"[OCRSRV] Unloading dedicated OCR server (PID {_ocr_process.pid}) after use - VRAM freed")
        proc = _ocr_process
        _ocr_process = None
        _last_model_path = None

    # Terminate OUTSIDE the lock: wait(timeout=5) + possible kill must not hold the
    # manager lock while a concurrent ensure_ocr_server() call is waiting on it.
    try:
        _terminate_process(proc)
    except Exception as e:
        logger.warning(f"[OCRSRV] Unload failed to terminate OCR process: {e}")

    # Wait until the port is actually free so a follow-up ensure_ocr_server() can start
    # cleanly (bounded - never blocks forever).
    import time as _time
    for _ in range(10):
        if not _is_healthy(_port()):
            logger.info(f"[OCRSRV] OCR server unloaded - port {_port()} free, VRAM released")
            return True
        _time.sleep(1)
    logger.warning(f"[OCRSRV] Process terminated but port {_port()} still answering after 10s - continuing anyway")
    return True


def shutdown_ocr_server() -> None:
    """Terminate the tracked OCR process (called on app exit / manual cleanup).

    Registered via atexit in code.py so a running dedicated instance never
    survives as an orphaned GPU process. Sync-safe - runs outside any event loop.
    """
    global _ocr_process, _last_model_path
    with _lock:  # short critical section - no blocking work inside
        proc = _ocr_process
        if proc is not None and proc.poll() is None:
            _ocr_process = None
            _last_model_path = None
        else:
            return  # nothing we launched is running - nothing to do

    from .process_mgmt import _terminate_process
    logger.info(f"[OCRSRV] Shutting down dedicated OCR server (PID {proc.pid})")
    try:
        _terminate_process(proc)   # blocking wait up to ~8 s, OUTSIDE the lock
    except Exception as e:
        logger.warning(f"[OCRSRV] Shutdown failed to terminate OCR process: {e}")