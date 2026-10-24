"""Standalone llama-node launcher (2026-10 fleet).

Runs on a GPU machine that is NOT the brain. It starts ONE llama-server with the same
settings the brain would use, verifies it, prints a summary + the brain entry, and EXITS
leaving llama-server running detached. The brain then attaches to it (attach-only) and
discovers its slots/model via /props.

Why a dedicated launcher instead of starting the whole SERVER here: a graceful SERVER
shutdown TERMINATES every loopback llama-server (that is correct on the brain, fatal on a
node), and a node does not need auth/queue/relay. This launcher only spawns + verifies.

Commands (see llama_node.py / ZZZ_LLAMA_NODE.bat):
    configure   interactive wizard -> config/llama_node.json
    start       idempotent: no-op if the right llama is already running, else (re)start
    stop        stop THIS node's llama-server (and record desired=stopped)
    status      probe /health + /props and print slots/ctx/model/build + brain entry
    restart     stop + start
    cmd         print the exact llama-server command (for a plain .bat, no Python)

`start` is reconciliation, not blind spawning: if a healthy llama on the configured port
already serves the configured model with the configured slot count, it does nothing -
which makes a scheduled task ("run start every 5 minutes") a free watchdog.
"""

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
from datetime import datetime
from typing import Optional

from config import (
    CONTEXT_WINDOW_TOKENS,
    LLAMA_MAX_CONCURRENCY,
    LLAMA_SERVER_GPU_LAYERS,
    LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC,
    LLAMA_SERVER_PARALLEL,
    LLAMA_SERVER_PORT,
    LLAMA_SERVER_STARTUP_WAIT_SEC,
    LLAMA_SERVER_THREADS,
    PROVIDER_STARTUP_TIMEOUT,
)
from .command_builder import _build_server_cmd
from .health_check import _verify_model_loaded
from .model_identity import model_basename, model_key
from .path_utils import _collect_all_gguf, _get_default_models_dir, _get_model_scan_dirs

logger = logging.getLogger("COOLEMS.Provider.Llama.Node")

CONFIG_REL = os.path.join("config", "llama_node.json")
EXAMPLE_REL = os.path.join("config", "llama_node.example.json")
STATE_REL = os.path.join("config", ".llama_node_state.json")
LOG_DIR_REL = "logs"

# Presets offered by the wizard. "ctx_per_slot": None means CONTEXT_WINDOW_TOKENS.
PRESETS = {
    "default": {"concurrency": 1, "ctx_per_slot": None,
                "label": "default (1 slot, full context) - same as the brain's local node"},
    "h200": {"concurrency": 2, "ctx_per_slot": None,
             "label": "H200 (2 slots, full context per slot)"},
    "5090": {"concurrency": 1, "ctx_per_slot": 65536,
             "label": "5090 / 32 GB (1 slot, 64k context)"},
    "custom": {"label": "custom (ask everything)"},
}


# ---------------------------------------------------------------------------
# Paths / small helpers
# ---------------------------------------------------------------------------

def _root() -> str:
    from .server import _resolve_server_root
    return _resolve_server_root()


def _path(rel: str) -> str:
    return os.path.join(_root(), rel)


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _read_state() -> dict:
    return _load_json(_path(STATE_REL)) or {}


def _write_state(**fields) -> dict:
    state = _read_state()
    state.update(fields)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save_json(_path(STATE_REL), state)
    return state


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        # Offline / no default route: fall back to a non-loopback interface address.
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127."):
                    return ip
        except Exception:
            pass
        return "127.0.0.1"


def _log_path(port: int) -> str:
    return os.path.join(_root(), LOG_DIR_REL, f"llama-node-{port}.log")


def _tail(path: str, lines: int = 20) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""


def _api(port: int) -> str:
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _ask(prompt: str, default=None, cast=str):
    """Prompt with a default; empty input accepts it. Raises RuntimeError if non-TTY."""
    if not sys.stdin.isatty():
        raise RuntimeError("interactive configuration needs a terminal (run the .bat)")
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
        except EOFError as e:
            raise RuntimeError("interactive configuration aborted (EOF)") from e
        if not raw:
            if default is not None:
                return default
            continue
        try:
            return cast(raw)
        except (TypeError, ValueError):
            print("  invalid value, try again")


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    raw = _ask(prompt + " (y/n)", "y" if default else "n", str).lower()
    return raw.startswith("y")


def _model_candidates() -> list[str]:
    try:
        return _collect_all_gguf(_get_model_scan_dirs(), _get_default_models_dir())
    except Exception as e:
        logger.debug("model scan failed: %s", e)
        return []


def _pick_model(existing: str = "") -> str:
    candidates = _model_candidates()
    if candidates:
        print("\nLocal .gguf models found:")
        for i, p in enumerate(candidates[:20], 1):
            print(f"  {i:2d}) {os.path.basename(p)}   ({p})")
        default = existing if existing else candidates[0]
        raw = _ask("Model (number or absolute path)", default, str)
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        return raw
    print("\nNo .gguf files found in the scan folders.")
    print("  scanned: " + ", ".join(_get_model_scan_dirs()))
    return _ask("Absolute path to the model .gguf", existing or "", str)


def configure() -> int:
    """Interactive wizard -> config/llama_node.json."""
    existing = load_raw_config() or {}
    if existing:
        print("\nCurrent configuration:")
        print(json.dumps(existing, indent=2))

    print("\nPresets:")
    keys = list(PRESETS.keys())
    for i, k in enumerate(keys, 1):
        print(f"  {i}) {k:8s} {PRESETS[k]['label']}")
    choice = _ask("Preset", "1", str)
    preset_name = keys[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(keys) else "default"
    preset = PRESETS[preset_name]

    name = _ask("Node name (used in the brain's llama_servers.json)", existing.get("name") or socket.gethostname(), str)
    bind = _ask("Bind address (0.0.0.0 = reachable from the brain)", existing.get("bind") or "0.0.0.0", str)
    port = _ask("HTTP port", existing.get("port") or LLAMA_SERVER_PORT, int)
    concurrency = _ask("Concurrency / --parallel slots (1-%d)" % LLAMA_MAX_CONCURRENCY,
                       preset.get("concurrency", existing.get("concurrency") or LLAMA_SERVER_PARALLEL), int)
    concurrency = max(1, min(int(concurrency), LLAMA_MAX_CONCURRENCY))

    ctx_default = existing.get("ctx_per_slot") or preset.get("ctx_per_slot") or CONTEXT_WINDOW_TOKENS
    ctx_per_slot = _ask("Context tokens PER SLOT (total = slots x this)", ctx_default, int)
    if ctx_per_slot <= 0:
        ctx_per_slot = CONTEXT_WINDOW_TOKENS

    threads = _ask("CPU threads", existing.get("threads") or LLAMA_SERVER_THREADS, int)
    gpu_layers = _ask("GPU layers (99 = all)", existing.get("gpu_layers")
                      if existing.get("gpu_layers") is not None else LLAMA_SERVER_GPU_LAYERS, int)
    model = _pick_model(existing.get("model") or "")

    extra = _ask("Extra llama-server args (space separated, optional)",
                 " ".join(existing.get("extra_args") or []), str)
    extra_args = extra.split() if extra else []

    cfg = {
        "name": name,
        "model": model,
        "bind": bind or "0.0.0.0",
        "port": int(port),
        "concurrency": concurrency,
        "ctx_per_slot": ctx_per_slot,
        "threads": threads,
        "gpu_layers": gpu_layers,
        "extra_args": extra_args,
    }
    _save_json(_path(CONFIG_REL), cfg)
    print(f"\n[NODE] configuration written to {_path(CONFIG_REL)}")
    print(json.dumps(cfg, indent=2))
    return 0


def load_raw_config() -> Optional[dict]:
    cfg = _load_json(_path(CONFIG_REL))
    if cfg is None:
        example = _load_json(_path(EXAMPLE_REL))
        if example is not None:
            logger.info("[NODE] %s missing; run 'configure' (example loaded for display only)",
                        _path(CONFIG_REL))
        return None
    return cfg


def load_config() -> dict:
    """Load + validate the node config (raises RuntimeError with an actionable message)."""
    raw = load_raw_config()
    if raw is None:
        raise RuntimeError(
            f"{_path(CONFIG_REL)} not found. Run 'llama_node.py configure' "
            f"(or ZZZ_LLAMA_NODE.bat -> Configure) on this machine first."
        )

    extra = raw.get("extra_args") or []
    if not isinstance(extra, list):
        raise RuntimeError(f"'extra_args' must be a list of strings in {_path(CONFIG_REL)}")

    cfg = {
        "name": str(raw.get("name") or socket.gethostname()).strip(),
        "model": str(raw.get("model") or "").strip(),
        "bind": str(raw.get("bind") or "0.0.0.0").strip(),
        "port": int(raw.get("port") or LLAMA_SERVER_PORT),
        "concurrency": int(raw.get("concurrency") or LLAMA_SERVER_PARALLEL),
        "ctx_per_slot": int(raw["ctx_per_slot"]) if raw.get("ctx_per_slot") else None,
        "threads": int(raw["threads"]) if raw.get("threads") else None,
        "gpu_layers": int(raw["gpu_layers"]) if raw.get("gpu_layers") is not None else None,
        "extra_args": [str(a) for a in extra],
    }

    if not (0 < cfg["port"] < 65536):
        raise RuntimeError(f"invalid port {cfg['port']} in {_path(CONFIG_REL)}")
    cfg["concurrency"] = max(1, min(cfg["concurrency"], LLAMA_MAX_CONCURRENCY))
    if cfg["ctx_per_slot"] is not None and cfg["ctx_per_slot"] < 256:
        raise RuntimeError(
            f"'ctx_per_slot' must be >= 256 (got {cfg['ctx_per_slot']}) in {_path(CONFIG_REL)}"
        )
    if not cfg["model"]:
        raise RuntimeError(f"'model' is empty in {_path(CONFIG_REL)} - run configure again")
    return cfg


def _resolve_model(cfg: dict) -> str:
    from .server import _resolve_model_path, _resolve_startup_model

    raw = cfg["model"]
    if os.path.isabs(raw) and os.path.exists(raw):
        return raw
    resolved = _resolve_model_path(raw)
    # STRICT: _resolve_model_path may fall back to "first available model" when the
    # requested name has no quant suffix. For a fleet node that would silently launch a
    # DIFFERENT model than the brain expects, so require the canonical key to match.
    if resolved and model_key(resolved) == model_key(raw):
        return resolved
    # Last resort: whatever the machine would boot by default, but only if it IS the
    # requested model (persisted path on another machine, etc.).
    fallback = _resolve_startup_model()
    if fallback and model_key(fallback) == model_key(raw):
        return fallback
    raise RuntimeError(
        f"model '{raw}' not found. Scanned: {', '.join(_get_model_scan_dirs())}. "
        f"Put the .gguf there or set an absolute path in {_path(CONFIG_REL)}."
    )


def _build_cmd(cfg: dict, model_path: str) -> list:
    from .server import _find_mmproj_file, _is_mtp_model

    exe = os.path.join(_root(), "llama_server", "llama-server.exe")
    if not os.path.exists(exe):
        raise RuntimeError(
            f"llama-server.exe not found at {exe}. Copy the llama_server/ folder from the "
            f"main machine (it is gitignored, ~1.3 GB) or run ZZZ_initial_init.bat here."
        )
    mmproj = _find_mmproj_file(model_path)
    is_mtp = _is_mtp_model(model_path)
    return _build_server_cmd(
        exe, model_path, mmproj, enable_mtp=is_mtp,
        host=cfg["bind"], port=cfg["port"], parallel=cfg["concurrency"],
        ctx_per_slot=cfg.get("ctx_per_slot"), threads=cfg.get("threads"),
        gpu_layers=cfg.get("gpu_layers"), extra_args=cfg.get("extra_args"),
    )


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

async def _fetch_props(port: int) -> Optional[dict]:
    from .http_client import get_async_client
    try:
        client = await get_async_client()
        r = await client.get(f"{_api(port)}/props", timeout=3.0)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


async def _healthy(port: int) -> bool:
    from .http_client import get_async_client
    try:
        client = await get_async_client()
        r = await client.get(f"{_api(port)}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def _props_match(cfg: dict, props: Optional[dict]) -> bool:
    if not props:
        return False
    if model_key(str(props.get("model_path") or props.get("model_alias") or "")) != model_key(cfg["model"]):
        return False
    slots = props.get("total_slots")
    if isinstance(slots, int) and slots != cfg["concurrency"]:
        return False
    return _ctx_matches(cfg, props)


def _reported_ctx(props: Optional[dict]) -> Optional[int]:
    """Per-slot context reported by llama (/props.default_generation_settings.n_ctx)."""
    if not props:
        return None
    dgs = props.get("default_generation_settings") or {}
    ctx = dgs.get("n_ctx") if isinstance(dgs, dict) else None
    if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
        return ctx
    return None


def _ctx_matches(cfg: dict, props: Optional[dict]) -> bool:
    """Compare the configured ctx/slot with the reported one, tolerating llama's rounding.

    llama.cpp rounds --ctx-size up to a KV cell boundary (256 tokens in practice), so
    181072 is reported as 181248. Anything from the requested value to +512 counts as a
    match; a real change (64k vs 181k) does not.
    """
    want = int(cfg.get("ctx_per_slot") or CONTEXT_WINDOW_TOKENS)
    got = _reported_ctx(props)
    if got is None:
        return True
    return want <= got < want + 512


# ---------------------------------------------------------------------------
# Start / stop / status
# ---------------------------------------------------------------------------

def _spawn_detached(cmd: list, log_path: str) -> subprocess.Popen:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log_path, "ab", buffering=0) as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            cwd=os.path.dirname(cmd[0]),
        )
    return proc


async def _wait_ready(cfg: dict, model_path: str, proc: subprocess.Popen) -> tuple[bool, str, Optional[dict]]:
    # Wait for /health, failing fast if the process exits (bad flags/OOM/port in use).
    for _ in range(PROVIDER_STARTUP_TIMEOUT):
        if proc.poll() is not None:
            return False, f"llama-server exited with code {proc.returncode} during startup", None
        if await _healthy(cfg["port"]):
            break
        await asyncio.sleep(LLAMA_SERVER_STARTUP_WAIT_SEC)
    else:
        return False, "llama-server did not answer /health in time", None

    api = _api(cfg["port"])
    attempts = max(1, LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC // 2)
    for i in range(attempts):
        if proc.poll() is not None:
            return False, f"llama-server exited with code {proc.returncode} while loading the model", None
        if await _verify_model_loaded(api, model_path):
            props = await _fetch_props(cfg["port"])
            if props is None:
                return False, ("the model loaded but /props is unavailable - the brain needs "
                               "/props (model, slots, ctx) to route to this node"), None
            got = model_key(str(props.get("model_path") or props.get("model_alias") or ""))
            if not got:
                return False, "/props has no model_path - the brain cannot route to this node", None
            if got != model_key(model_path):
                return False, (f"/props reports model '{props.get('model_path')}' but "
                               f"'{model_path}' was configured"), None
            return True, "ready", props
        if i % 10 == 0:
            logger.info("[NODE] waiting for model to load (attempt %d/%d)", i + 1, attempts)
        await asyncio.sleep(2)
    return False, "server healthy but the model did not finish loading in time", None


async def start(force: bool = False) -> int:
    cfg = load_config()
    state = _read_state()
    if state.get("desired") == "stopped" and not force:
        print(f"[NODE] '{cfg['name']}' was intentionally stopped. Use 'start --force' to run it.")
        return 0

    # Reconcile: is the desired llama already there?
    props = await _fetch_props(cfg["port"])
    if props:
        same_model = model_key(str(props.get("model_path") or props.get("model_alias") or "")) == model_key(cfg["model"])
        reported = props.get("total_slots")
        slots_match = (not isinstance(reported, int)) or reported == cfg["concurrency"]
        ctx_match = _ctx_matches(cfg, props)

        if same_model and slots_match and ctx_match:
            _write_state(desired="running", port=cfg["port"], name=cfg["name"],
                         model=cfg["model"], concurrency=cfg["concurrency"],
                         ctx_per_slot=cfg.get("ctx_per_slot"),
                         pid=state.get("pid"), log=_log_path(cfg["port"]))
            print(f"[NODE] '{cfg['name']}' already running on port {cfg['port']} "
                  f"(slots {reported}, ctx/slot {_reported_ctx(props)}, "
                  f"model {props.get('model_alias') or props.get('model_path')}) - nothing to do")
            return 0

        # Already launched by us with THIS configured slot/context count, but llama reports
        # something else (e.g. an older build that ignores --parallel, or ctx rounding
        # beyond our tolerance). Restarting would not change anything and a 5-minute
        # watchdog would reload the model forever - accept it and warn.
        if (same_model and state.get("pid")
                and state.get("concurrency") == cfg["concurrency"]
                and state.get("ctx_per_slot") == cfg.get("ctx_per_slot")
                and (not slots_match or not ctx_match)):
            _write_state(concurrency=cfg["concurrency"], ctx_per_slot=cfg.get("ctx_per_slot"),
                         effective_slots=reported, effective_ctx=_reported_ctx(props),
                         desired="running")
            print(f"[NODE] WARNING: requested slots={cfg['concurrency']} "
                  f"ctx/slot={cfg.get('ctx_per_slot') or CONTEXT_WINDOW_TOKENS}, llama reports "
                  f"slots={reported} ctx/slot={_reported_ctx(props)}. Keeping it running; "
                  f"the brain routes by the reported values.")
            return 0

    # A port with a listener but no /props is either a llama still LOADING or a foreign
    # process. Never kill blindly, and never spawn a second llama while ours is loading
    # (a cold 27B load can exceed the 5-minute watchdog interval).
    if props is None:
        from .process_mgmt import _find_pid_on_port
        pid = await asyncio.to_thread(_find_pid_on_port, cfg["port"])
        if pid:
            from .process_mgmt import _get_process_commandline
            cmdline = await asyncio.to_thread(_get_process_commandline, pid)
            if cmdline and "llama" in cmdline.lower():
                print(f"[NODE] llama-server (PID {pid}) is already starting/loading on port "
                      f"{cfg['port']} - not spawning another one. Check 'status' or wait.")
                return 0
            if await _healthy(cfg["port"]):
                raise RuntimeError(
                    f"port {cfg['port']} is served by a NON-llama process (PID {pid}). "
                    f"Free the port or change 'port' in {_path(CONFIG_REL)}."
                )
            raise RuntimeError(
                f"port {cfg['port']} is already in use by PID {pid}. Free it or change the node port."
            )
    elif props and not _props_match(cfg, props):
        print(f"[NODE] port {cfg['port']} runs a different llama configuration "
              f"(model={props.get('model_path')}, slots={props.get('total_slots')}) - restarting it")

    model_path = _resolve_model(cfg)
    cmd = _build_cmd(cfg, model_path)
    log_path = _log_path(cfg["port"])

    if props is not None:
        # Ours but misconfigured -> stop before respawn.
        await _stop_by_port(cfg["port"])

    print(f"[NODE] starting '{cfg['name']}' on {cfg['bind']}:{cfg['port']} "
          f"(slots={cfg['concurrency']}, ctx/slot={cfg.get('ctx_per_slot') or CONTEXT_WINDOW_TOKENS})")
    print("[NODE] command: " + subprocess.list2cmdline(cmd))
    proc = _spawn_detached(cmd, log_path)
    pid = proc.pid
    _write_state(desired="running", port=cfg["port"], name=cfg["name"], pid=pid,
                 model=cfg["model"], model_path=model_path,
                 concurrency=cfg["concurrency"], ctx_per_slot=cfg.get("ctx_per_slot"),
                 log=log_path, started_at=datetime.now().isoformat(timespec="seconds"))

    ok, message, ready_props = await _wait_ready(cfg, model_path, proc)
    if not ok:
        _write_state(desired="unknown", pid=None, last_error=message)
        print(f"[NODE] ERROR: {message}")
        tail = _tail(log_path)
        if tail:
            print("[NODE] last log lines:\n" + tail)
        await _stop_by_port(cfg["port"])
        return 1

    reported = ready_props.get("total_slots") if ready_props else None
    if isinstance(reported, int) and reported != cfg["concurrency"]:
        print(f"[NODE] WARNING: llama reports {reported} slot(s) but config asks for "
              f"{cfg['concurrency']} - the brain will clamp routing to {reported}.")

    _print_summary(cfg, model_path, log_path)
    return 0


def _print_summary(cfg: dict, model_path: str, log_path: str) -> None:
    print("\n" + "=" * 70)
    print(f"LLAMA NODE READY: {cfg['name']}")
    print("=" * 70)
    print(f"  bind        : {cfg['bind']}:{cfg['port']}  (LAN {_lan_ip()})")
    if str(cfg["bind"]).lower() in ("127.0.0.1", "localhost", "::1"):
        print("  WARNING     : bind is LOOPBACK - the brain cannot reach this node.")
        print("                Set \"bind\": \"0.0.0.0\" (or this machine's LAN IP) and restart.")
    print(f"  model       : {os.path.basename(model_path)}")
    print(f"  slots       : {cfg['concurrency']}   ctx/slot: {cfg.get('ctx_per_slot') or CONTEXT_WINDOW_TOKENS}")
    print(f"  log         : {log_path}")
    print(f"  llama-server keeps running after this launcher exits.")
    print("\nAdd this entry to config/llama_servers.json on the SERVER (brain):")
    print("  " + json.dumps({"name": cfg["name"], "host": _lan_ip(), "port": cfg["port"]}))
    print("  (no concurrency needed - the brain discovers it from /props)")
    print("=" * 70)


async def _stop_by_port(port: int) -> None:
    from .process_mgmt import _get_process_commandline, _find_pid_on_port, _kill_llama_server_by_port

    pid = await asyncio.to_thread(_find_pid_on_port, port)
    if pid:
        cmdline = await asyncio.to_thread(_get_process_commandline, pid)
        if cmdline is not None and "llama" not in cmdline.lower():
            raise RuntimeError(f"port {port} is PID {pid} ({cmdline[:80]}...) - not a llama-server; refusing to kill it")
    await asyncio.to_thread(_kill_llama_server_by_port, port)
    for _ in range(30):
        if not await _healthy(port):
            return
        await asyncio.sleep(0.5)


async def stop() -> int:
    state = _read_state()
    port = int(state.get("port") or LLAMA_SERVER_PORT)
    cfg = load_raw_config() or {}
    if cfg.get("port"):
        port = int(cfg["port"])

    # "Not healthy" is NOT the same as "not running": a llama that is still LOADING
    # answers /health with 503 and has no /props yet. Only declare it stopped when no
    # process listens on the port.
    if not await _healthy(port):
        from .process_mgmt import _find_pid_on_port
        pid = await asyncio.to_thread(_find_pid_on_port, port)
        if not pid:
            _write_state(desired="stopped", pid=None, port=port)
            print(f"[NODE] nothing is listening on port {port} - marked as stopped")
            return 0

    print(f"[NODE] stopping llama-server on port {port} ...")
    # _stop_by_port verifies the command line and refuses to kill a non-llama process.
    await _stop_by_port(port)
    if await _healthy(port):
        raise RuntimeError(
            f"llama-server on port {port} is STILL healthy after the stop attempt - "
            f"not marking it stopped. Check it by hand (PID/port)."
        )
    _write_state(desired="stopped", pid=None, port=port)
    print(f"[NODE] stopped. The 5-minute watchdog will NOT restart it until 'start --force'.")
    return 0


async def status() -> int:
    state = _read_state()
    raw = load_raw_config()
    port = int((raw or {}).get("port") or state.get("port") or LLAMA_SERVER_PORT)
    name = (raw or {}).get("name") or state.get("name") or "?"

    props = await _fetch_props(port)
    healthy = await _healthy(port)
    print("\n" + "=" * 70)
    print(f"LLAMA NODE STATUS: {name}  (port {port})")
    print("=" * 70)
    print(f"  health      : {'OK' if healthy else 'DOWN'}")
    print(f"  desired     : {state.get('desired') or '?'}")
    print(f"  pid         : {state.get('pid') or '?'}")
    print(f"  log         : {state.get('log') or _log_path(port)}")
    if props:
        print(f"  model       : {props.get('model_path') or props.get('model_alias')}")
        print(f"  slots       : {props.get('total_slots')}")
        ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
        print(f"  ctx/slot    : {ctx}")
        print(f"  build       : {props.get('build_info')}")
        print("\n  brain entry : " + json.dumps(
            {"name": name, "host": _lan_ip(), "port": port}))
    else:
        print("  props       : UNAVAILABLE - the brain cannot route to this node "
              "(llama.cpp build without /props?)")
    print("=" * 70)
    return 0 if (healthy and props) else 1


def print_cmd() -> int:
    cfg = load_config()
    model_path = _resolve_model(cfg)
    cmd = _build_cmd(cfg, model_path)
    print(subprocess.list2cmdline(cmd))
    return 0


async def restart(force: bool = True) -> int:
    try:
        await stop()
    except RuntimeError as e:
        print(f"[NODE] stop skipped: {e}")
    return await start(force=True)


# ---------------------------------------------------------------------------
# CLI dispatch (called by llama_node.py)
# ---------------------------------------------------------------------------

def dispatch(command: str, force: bool = False) -> int:
    try:
        if command == "configure":
            return configure()
        if command == "start":
            return asyncio.run(start(force=force))
        if command == "stop":
            return asyncio.run(stop())
        if command == "status":
            return asyncio.run(status())
        if command == "restart":
            return asyncio.run(restart())
        if command == "cmd":
            return print_cmd()
        raise RuntimeError(f"unknown command '{command}'")
    except RuntimeError as e:
        print(f"[NODE] ERROR: {e}")
        return 1
    except Exception as e:
        print(f"[NODE] ERROR: {type(e).__name__}: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
