"""Process lifecycle and cleanup for llama-server subprocesses.

Handles terminating, killing, and orphan detection for llama-server processes.
Includes stderr polling during startup to diagnose failures early.

Module-level globals track the running server process state across calls.
"""

import subprocess
import logging
import asyncio
from typing import Optional
from config import LLAMA_SERVER_PORT  # single source of truth (config/config.py) - no hardcoded port literals in this package

logger = logging.getLogger("COOLEMS.Provider.Llama.Server")


# ---------------------------------------------------------------------------
# Module-level state — shared with server.py via imports
# ---------------------------------------------------------------------------

# (2026-09-08 multi-chat) per-instance process registry, keyed by (host, port).
# Each llama-server instance from config/llama_servers.json tracks its own Popen so a
# reload of one instance never kills another. The legacy no-key API below maps to the
# default local key and keeps old call sites working unchanged.
from .model_state import key_for_api_url as _key_for_api_url  # noqa: E402  (same package, no cycle)

_DEFAULT_KEY = ("127.0.0.1", LLAMA_SERVER_PORT)  # legacy default (derived from config); server.py always passes the real key now
_server_processes: dict[tuple, Optional[subprocess.Popen]] = {}
# authoritative in-memory "which model is loaded" cache (used by the optimized
# AUTO-SWITCH check). Do NOT reintroduce per-module copies.


# ---------------------------------------------------------------------------
# Pipe drainers — CRITICAL FIX (b10441 hang)
#
# llama-server is started with stdout/stderr=PIPE. If nobody reads those pipes
# after startup, the OS pipe buffer fills up and llama.cpp BLOCKS on write().
# Symptom: GPU goes idle, every chat request hangs forever with no error.
# The old code never hit this because its log volume stayed under the buffer
# limit; b10441 logs more (speculative-decoding stats etc.) so it deadlocks.
#
# _start_pipe_drainers() spawns daemon threads that continuously read both
# pipes and forward every line to the COOLEMS logs — giving us full visibility
# into what llama-server is doing at all times.
# ---------------------------------------------------------------------------

import threading as _threading


def _drain_stream(stream, tag: str, process: subprocess.Popen) -> None:
    """Continuously read a subprocess pipe and log every line."""
    try:
        for raw in iter(stream.readline, b""):
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line.strip():
                logger.info(f"[LLAMA {tag}] (PID {process.pid}): {line}")
    except Exception as e:
        logger.debug(f"Pipe drainer ({tag}) stopped: {e}")
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _start_pipe_drainers(process: subprocess.Popen) -> None:
    """Start daemon threads that drain stdout+stderr of llama-server forever.

    Must be called right after Popen(). Prevents the pipe-buffer deadlock and
    gives us a live log of everything llama.cpp prints (model load, spec
    decoding stats, errors, OOM messages...).

    PHASE C LEAF (2026-09 threadless refactor): these daemon threads are the ONE deliberate
    exception to the no-threads rule. Blocking pipe reads have no asyncio equivalent on
    Windows (no non-blocking readline for subprocess pipes), and they must run FOREVER - a
    task-based drain would need an OS read primitive that does not exist in stdlib. They are
    daemon threads: pure I/O, no shared state, killed with the process. Everything else in
    this module is thread-free.
    """
    if process.stdout:
        t = _threading.Thread(
            target=_drain_stream, args=(process.stdout, "STDOUT", process),
            name=f"llama-stdout-drainer-{process.pid}", daemon=True,
        )
        t.start()
    if process.stderr:
        t = _threading.Thread(
            target=_drain_stream, args=(process.stderr, "STDERR", process),
            name=f"llama-stderr-drainer-{process.pid}", daemon=True,
        )
        t.start()
    logger.info(f"[LLAMA] Pipe drainers started for PID {process.pid} (stdout+stderr -> COOLEMS logs)")


def _poll_stderr(process: subprocess.Popen, attempt_num: int) -> None:
    """Poll stderr from a running process during startup wait loop.

    NOTE: when pipe drainers are active they own the pipes — reading here would
    race with them and could swallow lines, so this becomes a no-op in that case.
    The drainer threads already log every line to COOLEMS logs in real time.
    """
    if not process.stderr:
        return

    # Check if process already died (drainers handle the rest)
    if process.poll() is not None:
        logger.warning(f"[LLAMA STDERR] Process {process.pid} exited during startup wait (attempt {attempt_num}) — see [LLAMA STDOUT/STDERR] lines above for its last output")
        return

    # Drainers active -> they are logging everything; nothing to do here.
    logger.debug(f"[LLAMA] Startup wait attempt {attempt_num}: process still loading (pipe drainers forwarding live logs)")


def _check_stderr_early(process: subprocess.Popen) -> None:
    """Try to read stderr from a running process without killing it.

    NOTE: with pipe drainers active the pipes are owned by the drainer threads,
    so this is intentionally a no-op — any stderr output is already in the logs
    under [LLAMA STDERR].
    """
    logger.debug(f"[LLAMA] Early stderr check for PID {process.pid}: pipe drainers own the streams (see [LLAMA STDERR] log lines)")


def _log_process_error(process: subprocess.Popen) -> None:
    """Log final state of a process that failed to start.

    Does NOT terminate or kill the process - that is handled separately by
    _terminate_process(). With pipe drainers active, all output was already
    streamed to the logs in real time; here we just record the exit code so
    failures are easy to spot.
    """
    try:
        rc = process.poll()
        if rc is not None:
            logger.error(f"[LLAMA] Process {process.pid} exited with code {rc} — full output above in [LLAMA STDOUT]/[LLAMA STDERR] lines")
        else:
            # Still running but unhealthy (e.g. model load stuck). Drainers keep
            # logging; give a hint about where to look.
            logger.error(f"[LLAMA] Process {process.pid} still alive but failed health check — check [LLAMA STDOUT]/[LLAMA STDERR] lines above for the cause (VRAM? OOM? bad flag?)")
    except Exception as outer_e:
        logger.warning("Failed to capture process error state: %s", outer_e)


def _terminate_process(process: subprocess.Popen) -> None:
    """Safely terminate a subprocess and wait for it to exit.

    Clears this instance's entry in the per-key process registry when it matches.
    """

    try:
        logger.info(f"Terminating llama-server process (PID: {process.pid})")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning(f"Process {process.pid} did not terminate gracefully, forcing kill")
            process.kill()
            process.wait(timeout=3)
        logger.info(f"Process {process.pid} terminated successfully")
    except ProcessLookupError:
        logger.info(f"Process {process.pid} already terminated")
    except Exception as e:
        logger.warning(f"Failed to terminate process {process.pid}: {e}")
    finally:
        # Clear this instance's registry entry when it matches the terminated process.
        for _k, _p in list(_server_processes.items()):
            if _p is process:
                del _server_processes[_k]


def _kill_orphaned_process(key=None) -> None:
    """Kill any previously tracked server process before starting a new one.

    (2026-09-08 multi-chat) *key* selects the instance ((host, port)); legacy no-key
    calls target the default local key so old call sites keep working unchanged."""

    proc = _server_processes.get(key or _DEFAULT_KEY)
    if proc is not None:
        _terminate_process(proc)


def _kill_llama_server_by_port(port: int) -> None:
    """Kill any process listening on the llama server port (fallback for untracked processes)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if len(parts) >= 5:
                    pid = parts[-1]
                    try:
                        pid = int(pid)
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/F"],
                            capture_output=True, timeout=5
                        )
                        logger.info(f"[RELOAD] Killed process {pid} on port {port}")
                    except (ValueError, subprocess.TimeoutExpired):
                            logger.debug("Llama reload: skipped invalid PID during port cleanup")
    except Exception as e:
        logger.warning(f"[RELOAD] Could not kill process by port {port}: {e}")


async def wait_for_port_free_async(port: int) -> bool:
    """Wait for a TCP port to become free. Used during model reload.

    (2026-09 threadless refactor): async - polls with await asyncio.sleep() on the
    caller's loop instead of blocking time.sleep(); the connect_ex probe itself is a
    non-blocking socket call."""
    import socket as _socket

    # Wait for port to actually become free (max 30s)
    for _wait_i in range(15):
        await asyncio.sleep(2)
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _sock:
            if _sock.connect_ex(('127.0.0.1', port)) != 0:
                logger.info(f"[RELOAD] Port {port} is now free (attempt {_wait_i + 1})")
                return True

    # Force aggressive kill one more time
    logger.warning(f"[RELOAD] Port {port} still in use after 30s, forcing aggressive kill")
    await asyncio.to_thread(_kill_llama_server_by_port, port)
    await asyncio.sleep(3)
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _sock:
        if _sock.connect_ex(('127.0.0.1', port)) != 0:
            logger.info(f"[RELOAD] Port {port} is now free after forced kill")
            return True

    return False

def _set_server_process(process: Optional[subprocess.Popen], key=None) -> None:
    """Set the tracked server process."""

    key = key or _DEFAULT_KEY
    _server_processes[key] = process


def _get_server_process(key=None) -> Optional[subprocess.Popen]:
    """Get the tracked server process."""
    return _server_processes.get(key or _DEFAULT_KEY)


# 2026-08-18: removed dead accessors (_set/_get_last_loaded_model[_full_path]).
# They were never imported anywhere; model_state.py is now the single source of
# truth for "which model is loaded".