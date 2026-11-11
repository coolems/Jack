"""Llama fleet manager (2026-11 multi-config fleet).

ONE file drives every llama-server this machine (or LAN) runs:
``config/llama_servers.json`` — a JSON LIST of complete configs, one per llama-service:
name, host, port, model, slots, ctx/slot, threads, gpu layers, bind, extra flags, auto_start.
That is what makes "three llama-services on three ports, three different models, on one
machine" a normal thing to do, as long as the RAM is there.

TWO WAYS TO USE THIS MODULE:
  * ADMIN path   : utils/start_llama_node/llama_node.py + ZZZ_LLAMA_NODE.bat — a menu that
                   lists every config, configures/adds one, starts/stops/restarts/shows the
                   command of any single entry, and deletes one.
  * REGULAR path : the SERVER boot calls reconcile() for each LOCAL entry of the file
                   (backend_registry.LlmBackend.ensure_started). Healthy matching node ->
                   ATTACH; nothing on the port -> spawn detached IF THE MEMORY GATE ALLOWS IT.

Rules that never change:
  * MEMORY FIRST. Before any spawn, memory.check_launch() asks whether the model fits in the
    free RAM/VRAM. If Strata or another llama already ate the machine, we REPORT and stop -
    loading anyway would OOM-kill everything.
  * NEVER KILL WHAT WE DID NOT START. A node we attached to (admin launcher, manual start)
    survives SERVER shutdown; a non-llama process on a configured port is never killed.
  * A PORT WITH A DIFFERENT MODEL IS A CONFLICT, REPORTED - not silently reloaded.

Commands (llama_node.py / ZZZ_LLAMA_NODE.bat):
    list                every config + whether it is running
    configure [name]    wizard: edit an existing entry or add a new one (writes the file)
    start <name>        idempotent: attach if the right llama is there, else spawn
    stop <name>         stop that entry's llama-server (records desired=stopped)
    status [name]       probe /health + /props, print slots/ctx/model/build
    restart <name>      stop + start
    cmd <name>          print the exact llama-server command (for a plain .bat, no Python)
    delete <name>       remove an entry from the file
    start-all           start every local entry with auto_start=true (memory-gated, in order)

`start` is reconciliation, not blind spawning: a healthy llama on the configured port that
already serves the configured model does nothing - which makes a scheduled task ("run start
every 5 minutes") a free watchdog.
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
    LLAMA_SERVERS_FILE,
    PROVIDER_STARTUP_TIMEOUT,
    LlamaServerInstance,
    get_llama_server_entries_raw,
    get_llama_server_instances,
    instance_to_entry,
    save_llama_server_instances,
)
from .command_builder import _build_server_cmd
from .health_check import _verify_model_loaded
from .model_identity import model_key
from .model_utils import _is_mtp_model
from .path_utils import _collect_all_gguf, _get_default_models_dir, _get_model_scan_dirs

logger = logging.getLogger("COOLEMS.Provider.Llama.Node")

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
    "small": {"concurrency": 1, "ctx_per_slot": 16384,
              "label": "small / low RAM (1 slot, 16k context)"},
    "custom": {"label": "custom (ask everything)"},
}


def _emit(message: str) -> None:
    """Console line for the launcher AND the same line in the COOLEMS log.

    This module is BOTH an interactive admin CLI (utils/start_llama_node) and a module the
    SERVER calls at boot, so every line must go to stdout AND to logs/coolems.log. Console
    output uses print(); the logger writes the log file and, inside a SERVER run, the
    COOLEMS console handler.
    """
    print(message, flush=True)
    # Most call sites already carry the [NODE] tag; strip it before logging so the log
    # file never shows "[NODE] [NODE] ...".
    logger.info("[NODE] %s", message[6:].lstrip() if message.startswith("[NODE]") else message)


# ---------------------------------------------------------------------------
# Paths / small helpers
# ---------------------------------------------------------------------------

def _root() -> str:
    from .path_utils import _resolve_server_root
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


def _state_path() -> str:
    return _path(STATE_REL)


def _read_state() -> dict:
    """Per-NAME runtime state: {name: {desired, pid, port, model, ...}, updated_at}."""
    return _load_json(_state_path()) or {}


def _entry_state(name: str) -> dict:
    return _read_state().get(name) or {}


def _write_state(entry_name: str, **fields) -> dict:
    """Update one config's runtime block (keyed by its name, not by one global state)."""
    state = _read_state()
    block = state.get(entry_name) or {}
    block.update(fields)
    state[entry_name] = block
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save_json(_state_path(), state)
    return block


def _forget_state(name: str) -> None:
    state = _read_state()
    state.pop(name, None)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save_json(_state_path(), state)


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
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


def _api(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _ctx_of(props: Optional[dict]) -> Optional[int]:
    if not props:
        return None
    dgs = props.get("default_generation_settings") or {}
    ctx = dgs.get("n_ctx") if isinstance(dgs, dict) else None
    if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
        return ctx
    return None


def _props_model(props: Optional[dict]) -> str:
    if not props:
        return ""
    return str(props.get("model_path") or props.get("model_alias") or "")


def _ctx_matches(inst: LlamaServerInstance, props: Optional[dict]) -> bool:
    """Compare configured ctx/slot with the reported one, tolerating llama's rounding.

    llama.cpp rounds --ctx-size up to a KV cell boundary (256 tokens in practice), so
    181072 is reported as 181248. Anything from the requested value to +512 matches; a
    real change (64k vs 181k) does not.
    """
    want = int(inst.ctx_per_slot or CONTEXT_WINDOW_TOKENS)
    got = _ctx_of(props)
    if got is None:
        return True
    return want <= got < want + 512


# ---------------------------------------------------------------------------
# Config access (config/llama_servers.json is the single source of truth)
# ---------------------------------------------------------------------------

def list_configs() -> list[LlamaServerInstance]:
    """The RUNTIME fleet view, in file order (missing file -> re-seeded default).

    Duplicate host:port pairs are deduped FIRST-WINS - the machine can only run one
    server at one address, so boot/auto-start/fleet tracking use this view.
    """
    return get_llama_server_instances()


def all_configs() -> list[LlamaServerInstance]:
    """EVERY entry in the file, INCLUDING duplicate host:port pairs, in file order.

    The admin menu (list / configure / delete) works on this view: a shadowed duplicate
    must stay visible and editable - otherwise editing or deleting its twin would silently
    drop it from the file, and it could never be fixed.
    """
    return get_llama_server_entries_raw()


def find(name: str) -> LlamaServerInstance:
    """One entry by name (case-insensitive), or by 'host:port', or index 1-based.

    Searches the FULL file (all_configs), so a shadowed duplicate is still addressable -
    its index matches what the list page shows. Raises RuntimeError with the available
    names when nothing matches - an exact name is a name, and guessing at shortened ones
    would start the wrong model.
    """
    entries = all_configs()
    needle = (name or "").strip().lower()
    if needle:
        for inst in entries:
            if inst.name.lower() == needle:
                return inst
        if needle.isdigit():
            i = int(needle) - 1
            if 0 <= i < len(entries):
                return entries[i]
        for inst in entries:
            if f"{inst.host}:{inst.port}" == needle or f"{inst.host}:{inst.port}".lower() == needle:
                return inst
    raise RuntimeError(
        f"no config named '{name}'. Available: "
        + ", ".join(f"{i.name} ({i.host}:{i.port})" for i in entries)
    )


def _save(entries: list[LlamaServerInstance]) -> bool:
    return save_llama_server_instances(entries)


def _replace_or_add(entries: list[LlamaServerInstance], inst: LlamaServerInstance,
                    old_name: str = "") -> list[LlamaServerInstance]:
    """Replace one entry by name, or append it. Keeps file order stable.

    *old_name* is the name the wizard started editing: when the admin renames the entry,
    the OLD name is the one that must be replaced - otherwise a rename would leave the old
    entry in the file and add a second one on the same port.
    """
    match = (old_name or inst.name).lower()
    for i, e in enumerate(entries):
        if e.name.lower() == match:
            entries[i] = inst
            return entries
    entries.append(inst)
    return entries


def _prev(inst: Optional[LlamaServerInstance], attr: str, fallback):
    """Wizard default for one field: the entry's current value when editing, the fallback
    when ADDING a new config (there *existing* is None - reading existing.model on it is
    what crashed the wizard with "AttributeError: 'NoneType' object has no attribute
    'model'"). A real 0 (gpu_layers=0 = CPU only) is kept, not replaced by the fallback.
    """
    if inst is None:
        return fallback
    value = getattr(inst, attr, None)
    return fallback if value is None else value


def _endpoint(host: str, port: int) -> tuple[str, int]:
    """The identity the loader dedupes on: 'localhost' and '127.0.0.1' are one endpoint."""
    h = host.lower()
    return ("127.0.0.1" if h == "localhost" else h, int(port))


def _port_clash(entries: list[LlamaServerInstance], port: int, keep_names,
                host: str = "127.0.0.1") -> Optional[LlamaServerInstance]:
    """Another config that already claims this host:port.

    *keep_names* are the names of the entry being edited (its old name + the new one the
    admin typed) - those are the SAME config, so they are not a clash. The loader dedupes
    host:port (first entry wins), so saving a second config on a configured endpoint would
    silently drop one of them: the wizard refuses the save instead of writing a file that
    loses an entry.
    """
    ignore = {str(n).lower() for n in
              (keep_names if isinstance(keep_names, (tuple, list, set)) else (keep_names,)) if n}
    key = _endpoint(host, port)
    for e in entries:
        if e.name.lower() in ignore:
            continue
        if _endpoint(e.host, e.port) == key:
            return e
    return None


def _ask(prompt: str, default=None, cast=str):
    """Prompt with a default; empty input accepts it.

    No isatty() guard: the .bat menu runs this on a console, and a redirected/piped stdin
    is equally valid (a scheduled task can feed answers). A stdin that runs out raises
    instead of looping forever, so an aborted wizard is a clear error, not a hang.
    """
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        try:
            raw = input(f"{prompt}{suffix}: ").strip()
        except EOFError as e:
            raise RuntimeError("interactive configuration aborted (no more input)") from e
        if not raw:
            if default is not None:
                return default
            continue
        try:
            return cast(raw)
        except (TypeError, ValueError):
            _emit("  invalid value, try again")


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
        _emit("\nLocal .gguf models found:")
        for i, p in enumerate(candidates[:20], 1):
            _emit(f"  {i:2d}) {os.path.basename(p)}   ({p})")
        default = existing if existing else candidates[0]
        raw = _ask("Model (number, absolute path, or filename)", default, str)
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        return raw
    _emit("\nNo .gguf files found in the scan folders.")
    _emit("  scanned: " + ", ".join(_get_model_scan_dirs()))
    return _ask("Absolute path to the model .gguf", existing or "", str)


def configure(name: str = "") -> int:
    """Interactive wizard: edit the entry named *name*, or add a new one.

    Writes the WHOLE list back to config/llama_servers.json atomically, so other entries
    are never lost. Remote entries (host not loopback) are configured as attach-only: the
    brain never spawns a process on another machine.

    Admin-menu convention: an EMPTY name means "new config" (the .bat page sends that when
    nothing is typed via CLI passthrough), and a name that is NOT in the list becomes the
    NAME of the new config - the wizard pre-fills it instead of erroring.
    """
    # all_configs (NOT the deduped runtime view): saving must write back EVERY entry in
    # the file, or a shadowed duplicate would be silently dropped on any edit.
    entries = all_configs()
    existing = None
    new_name = ""
    if name:
        try:
            existing = find(name)
        except RuntimeError:
            # Unknown name = "create a config with THIS name" (the page prompt says so).
            new_name = str(name).strip()

    if existing:
        _emit(f"\nEditing config '{existing.name}' ({existing.host}:{existing.port}):")
        _emit(json.dumps(instance_to_entry(existing), indent=2))
    else:
        _emit("\nAdding a NEW config to config/llama_servers.json"
              + (f" (name: {new_name})" if new_name else ""))

    _emit("\nPresets:")
    keys = list(PRESETS.keys())
    for i, k in enumerate(keys, 1):
        _emit(f"  {i}) {k:8s} {PRESETS[k]['label']}")
    # Editing: Enter means "keep what is shown above" - a preset must never silently
    # rewrite an existing config. Adding: preset 1 is the sensible starting point.
    choice = _ask("Preset (Enter = keep the values above)" if existing else "Preset",
                  "" if existing else "1", str)
    preset = PRESETS[keys[int(choice) - 1]] if (choice.isdigit() and 1 <= int(choice) <= len(keys)) else None

    def _default(attr: str, fallback):
        """Answer default for one field: an explicitly chosen preset wins, then the entry's
        own value (a NEW config has none, so the fallback)."""
        if preset and preset.get(attr) is not None:
            return preset[attr]
        return _prev(existing, attr, fallback)

    default_name = new_name or _prev(existing, "name", socket.gethostname())
    name_out = _ask("Config name (used by list/start/stop/status)", default_name, str)

    host = _ask("Host (127.0.0.1 = this machine; a LAN IP = attach-only remote node)",
                _prev(existing, "host", "127.0.0.1"), str)
    port = _ask("HTTP port", _prev(existing, "port", LLAMA_SERVER_PORT), int)
    if not (0 < int(port) < 65536):
        _emit(f"[NODE] invalid port {port}")
        return 1
    clash = _port_clash(entries, int(port),
                        (existing.name if existing else "", name_out), host)
    if clash:
        # A duplicate port is a WARNING, never a refusal - the user may save any
        # physically valid port. But the loader dedupes host:port FIRST-WINS at load
        # time, so say plainly that one of the two will be shadowed.
        _emit(f"[NODE] WARNING: config '{clash.name}' already uses {host}:{port} - saving "
              f"anyway, but the loader keeps only ONE entry per host:port (first in the "
              f"file wins), so one of them will not be reachable under its own name.")

    loopback = host.lower() in ("127.0.0.1", "localhost", "::1")
    concurrency = None
    ctx_per_slot = None
    threads = None
    gpu_layers = None
    model = None
    bind = None
    extra_args: list[str] = []
    auto_start = True

    if loopback:
        concurrency = _ask("Concurrency / --parallel slots (1-%d)" % LLAMA_MAX_CONCURRENCY,
                           _default("concurrency", LLAMA_SERVER_PARALLEL), int)
        concurrency = max(1, min(int(concurrency), LLAMA_MAX_CONCURRENCY))

        ctx_per_slot = _ask("Context tokens PER SLOT (total = slots x this)",
                            _default("ctx_per_slot", CONTEXT_WINDOW_TOKENS), int)
        if ctx_per_slot < 256:
            ctx_per_slot = CONTEXT_WINDOW_TOKENS

        threads = _ask("CPU threads", _prev(existing, "threads", LLAMA_SERVER_THREADS), int)
        gpu_layers = _ask("GPU layers (99 = all, 0 = CPU only)",
                          _prev(existing, "gpu_layers", LLAMA_SERVER_GPU_LAYERS), int)
        model = _pick_model(_prev(existing, "model", ""))
        bind = _ask("Bind address (127.0.0.1 = local only, 0.0.0.0 = reachable on LAN)",
                    _prev(existing, "bind", "127.0.0.1"), str)
        extra = _ask("Extra llama-server args (space separated, optional)",
                     " ".join(_prev(existing, "extra_args", [])), str)
        extra_args = extra.split() if extra else []
        auto_start = _ask_yes_no(
            "Should the SERVER auto-start this config when it boots and finds nothing on its port?",
            default=_prev(existing, "auto_start", True))
    else:
        _emit("\nRemote endpoint: attach-only. The brain never spawns or kills it - its own")
        _emit("machine owns the model, slots and context; we discover them from /props.")

    inst = LlamaServerInstance(
        name=name_out, host=host, port=int(port), concurrency=concurrency,
        ctx_per_slot=ctx_per_slot, model=model, bind=bind, threads=threads,
        gpu_layers=gpu_layers, extra_args=tuple(extra_args), auto_start=bool(auto_start),
        kind="llama",
    )

    if not _save(_replace_or_add(entries, inst, existing.name if existing else "")):
        _emit("[NODE] ERROR: could not write config/llama_servers.json")
        return 1

    # A rename moves the runtime state too, so 'desired/pid/log' follow the config instead
    # of staying under the old name (where nothing would ever read them again).
    if existing and existing.name.lower() != inst.name.lower():
        moved = _read_state().get(existing.name) or {}
        if moved:
            _forget_state(existing.name)
            _write_state(inst.name, **moved)
        _emit(f"[NODE] renamed '{existing.name}' -> '{inst.name}' (runtime state moved with it)")

    _emit(f"\n[NODE] saved '{inst.name}' in {_path(os.path.join('config', 'llama_servers.json'))}")
    _emit(json.dumps(instance_to_entry(inst), indent=2))
    _emit(f"\nTotal configs now: {len(entries)}  (list them with 'list')")
    if loopback:
        _emit(f"Start it with: ZZZ_LLAMA_NODE.bat start {inst.name}")
    else:
        _emit("Start the llama-server on THAT machine (utils/start_llama_node there), then")
        _emit("the brain attaches to it and discovers its model.")
    return 0


def _resolve_model(inst: LlamaServerInstance) -> str:
    """STRICT model resolution for an entry - never silently a different model."""
    from .server import _resolve_entry_model
    return _resolve_entry_model(inst)


def _build_cmd(inst: LlamaServerInstance, model_path: str) -> list:
    from .server import _find_mmproj_file, _is_mtp_model

    exe = os.path.join(_root(), "llama_server", "llama-server.exe")
    if not os.path.exists(exe):
        raise RuntimeError(
            f"llama-server.exe not found at {exe}. Copy the llama_server/ folder from the "
            f"main machine (it is gitignored, ~1.3 GB) or run ZZZ_initial_init.bat here."
        )
    mmproj = _find_mmproj_file(model_path)
    is_mtp = _is_mtp_model(model_path)
    bind = inst.bind or "127.0.0.1"
    return _build_server_cmd(
        exe, model_path, mmproj, enable_mtp=is_mtp,
        host=bind if bind.lower() not in ("localhost", "127.0.0.1") else "0.0.0.0",
        port=inst.port, parallel=max(1, int(inst.concurrency or LLAMA_SERVER_PARALLEL)),
        ctx_per_slot=inst.ctx_per_slot, threads=inst.threads,
        gpu_layers=inst.gpu_layers, extra_args=list(inst.extra_args),
    )


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

async def _fetch_props(host: str, port: int) -> Optional[dict]:
    from .http_client import get_async_client
    try:
        client = await get_async_client()
        r = await client.get(f"{_api(host, port)}/props", timeout=3.0)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


async def _healthy(host: str, port: int) -> bool:
    from .http_client import get_async_client
    try:
        client = await get_async_client()
        r = await client.get(f"{_api(host, port)}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


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


async def _wait_ready(inst: LlamaServerInstance, model_path: str,
                      proc: subprocess.Popen) -> tuple[bool, str, Optional[dict]]:
    # Wait for /health, failing fast if the process exits (bad flags/OOM/port in use).
    for _ in range(PROVIDER_STARTUP_TIMEOUT):
        if proc.poll() is not None:
            return False, f"llama-server exited with code {proc.returncode} during startup", None
        if await _healthy(inst.host, inst.port):
            break
        await asyncio.sleep(LLAMA_SERVER_STARTUP_WAIT_SEC)
    else:
        return False, "llama-server did not answer /health in time", None

    api = _api(inst.host, inst.port)
    attempts = max(1, LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC // 2)
    for i in range(attempts):
        if proc.poll() is not None:
            return False, f"llama-server exited with code {proc.returncode} while loading the model", None
        if await _verify_model_loaded(api, model_path):
            props = await _fetch_props(inst.host, inst.port)
            if props is None:
                return False, ("the model loaded but /props is unavailable - the brain needs "
                               "/props (model, slots, ctx) to route to this node"), None
            got = model_key(_props_model(props))
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


async def _conflict_note(port: int, want_model_key: str) -> str:
    """Who else in the file wants this port / this model - for an actionable refusal."""
    lines = []
    for inst in list_configs():
        if inst.port == port and inst.is_local:
            lines.append(f"config '{inst.name}' also uses port {port} (model {inst.model or 'any'})")
        if want_model_key and inst.model and model_key(inst.model) == want_model_key:
            lines.append(f"config '{inst.name}' already runs that model on port {inst.port}")
    return "; ".join(lines)


async def reconcile(inst: LlamaServerInstance, allow_override: bool = False,
                    explicit: bool = False) -> tuple[bool, bool]:
    """SERVER-boot reconcile for ONE entry: attach if healthy+matching, else (maybe) spawn.

    *allow_override* (explicit human 'start' from a TTY): when the memory gate fails, ask
    once whether to start anyway. Boot/auto-start never pass it - there is no human to
    confirm, and a non-TTY run must never hang on input().

    *explicit*: a HUMAN typed 'start <name>' (menu or CLI). auto_start=false then means
    "the server boot should not spawn this one" - it does NOT refuse an explicit start.
    The memory gate still applies, so the refusal you can get here is only "not enough
    RAM/VRAM", never a pointer back at itself.

    Returns ``(spawned_by_us, ok)``:
      spawned_by_us=True  -> WE launched llama-server; the SERVER owns it and may stop it.
      spawned_by_us=False -> attached to a node started OUTSIDE us - never killed by us.

    Never raises: every failure is reported as ok=False so boot continues and the health
    poller retries.
    """
    if not inst.is_spawnable:
        ok = await _healthy(inst.host, inst.port)
        if not ok:
            _emit(f"[NODE] '{inst.name}' ({inst.api_url}) is not reachable - attach-only, "
                  f"nothing to spawn here")
        return False, ok

    if not inst.auto_start and not explicit:
        props = await _fetch_props(inst.host, inst.port)
        if props and model_key(_props_model(props)) == model_key(inst.model or ""):
            _emit(f"[NODE] '{inst.name}' auto_start=false - attaching (model="
                  f"{props.get('model_path')}, slots={props.get('total_slots')})")
            return False, True
        if props:
            _emit(f"[NODE] '{inst.name}' auto_start=false but port {inst.port} serves "
                  f"'{_props_model(props)}', not '{inst.model or 'the configured model'}' - not attaching")
            return False, False
        _emit(f"[NODE] '{inst.name}' auto_start=false and nothing is on port {inst.port} - "
              f"NOT spawning. Start it with ZZZ_LLAMA_NODE.bat start {inst.name}")
        return False, False

    try:
        model_path = _resolve_model(inst)
    except RuntimeError as e:
        _emit(f"[NODE] ERROR: {e}")
        _write_state(inst.name, desired="error", last_error=str(e))
        return False, False

    state = _entry_state(inst.name)
    if state.get("desired") == "stopped":
        # The admin explicitly stopped this config (menu item 5). A SERVER boot must not
        # relaunch it - but an explicit 'start <name>' clears that first (dispatch passes
        # force=True), so a user who wants to start gets a start, never a refusal.
        _emit(f"[NODE] '{inst.name}' is stopped. Start it with "
              f"'ZZZ_LLAMA_NODE.bat start {inst.name}'")
        return False, False

    slots = max(1, int(inst.concurrency or LLAMA_SERVER_PARALLEL))
    props = await _fetch_props(inst.host, inst.port)

    if props:
        same_model = model_key(_props_model(props)) == model_key(model_path)
        reported = props.get("total_slots")
        if same_model:
            if isinstance(reported, int) and reported != slots:
                _emit(f"[NODE] WARNING: '{inst.name}' asks for {slots} slot(s), llama reports "
                      f"{reported} - routing follows the reported value (restart to change it)")
            if not _ctx_matches(inst, props):
                _emit(f"[NODE] WARNING: '{inst.name}' asks for ctx/slot "
                      f"{inst.ctx_per_slot or CONTEXT_WINDOW_TOKENS}, llama reports "
                      f"{_ctx_of(props)} - routing follows the reported value")
            _write_state(inst.name, desired="running", port=inst.port, model=inst.model,
                         model_path=model_path, concurrency=slots,
                         ctx_per_slot=inst.ctx_per_slot, effective_slots=reported,
                         effective_ctx=_ctx_of(props), log=_log_path(inst.port))
            _emit(f"[NODE] '{inst.name}' already running on port {inst.port} "
                  f"(slots {reported}, ctx/slot {_ctx_of(props)}, model "
                  f"{props.get('model_alias') or props.get('model_path')}) - nothing to do")
            return False, True

        # Port occupied by a DIFFERENT model. Only stop what WE started for this name.
        if state.get("pid"):
            _emit(f"[NODE] '{inst.name}': port {inst.port} runs '{_props_model(props)}' "
                  f"(we started it) - stopping it and relaunching with '{os.path.basename(model_path)}'")
            await _stop_by_port(inst.port)
        else:
            _emit(f"[NODE] REFUSED '{inst.name}': port {inst.port} is already serving "
                  f"'{_props_model(props)}' and we did not start it. "
                  f"{await _conflict_note(inst.port, model_key(model_path))}. "
                  f"Stop that node first (ZZZ_LLAMA_NODE.bat stop <name>) or change this port.")
            return False, False

    elif not await _healthy(inst.host, inst.port):
        from .process_mgmt import _pids_listening_on_port, _get_process_commandline
        pids = await asyncio.to_thread(_pids_listening_on_port, inst.port)
        if pids:
            pid = pids[0]
            cmdline = await asyncio.to_thread(_get_process_commandline, pid)
            if cmdline and "llama" in cmdline.lower():
                _emit(f"[NODE] llama-server (PID {pid}) is already starting/loading on port "
                      f"{inst.port} - not spawning another one. Check 'status {inst.name}' or wait.")
                return False, False
            _emit(f"[NODE] REFUSED '{inst.name}': port {inst.port} is used by PID {pid} "
                  f"({(cmdline or 'unknown process')[:80]}) - not a llama-server; we will not kill it.")
            return False, False

    # ---- MEMORY GATE: refuse instead of loading and going out of memory. ----
    from . import memory
    is_mtp = _is_mtp_model(model_path)
    ok, budget = memory.check_launch(
        model_path, slots=slots,
        ctx_per_slot=inst.ctx_per_slot or CONTEXT_WINDOW_TOKENS,
        gpu_layers=inst.gpu_layers, mtp=is_mtp,
    )
    if not ok:
        _emit(f"[NODE] '{inst.name}' would exceed the memory budget:\n{budget}")
        overridden = False
        if allow_override and sys.stdin.isatty():
            overridden = _ask_yes_no("Start anyway? (the machine may run out of memory)",
                                     default=False)
        if not overridden:
            _write_state(inst.name, desired="refused", last_error=budget)
            _emit(f"[NODE] REFUSED '{inst.name}' - not starting. Free VRAM/RAM or lower "
                  f"ctx/slots, then start again.")
            return False, False
        _emit(f"[NODE] WARNING: '{inst.name}' - starting despite the memory gate (your call).")
    else:
        _emit(f"[NODE] memory budget OK for '{inst.name}': {budget}")

    cmd = _build_cmd(inst, model_path)
    log_path = _log_path(inst.port)
    _emit(f"[NODE] starting '{inst.name}' on {inst.bind or '127.0.0.1'}:{inst.port} "
          f"(slots={slots}, ctx/slot={inst.ctx_per_slot or CONTEXT_WINDOW_TOKENS})")
    _emit("[NODE] command: " + subprocess.list2cmdline(cmd))
    proc = _spawn_detached(cmd, log_path)

    _write_state(inst.name, desired="running", port=inst.port, pid=proc.pid, model=inst.model,
                 model_path=model_path, concurrency=slots, ctx_per_slot=inst.ctx_per_slot,
                 log=log_path, started_at=datetime.now().isoformat(timespec="seconds"))

    ready, message, ready_props = await _wait_ready(inst, model_path, proc)
    if not ready:
        _write_state(desired="unknown", pid=None, last_error=message)
        _emit(f"[NODE] ERROR: {message}")
        tail = _tail(log_path)
        if tail:
            _emit("[NODE] last log lines:\n" + tail)
        await _stop_by_port(inst.port)
        return False, False

    reported = ready_props.get("total_slots") if ready_props else None
    if isinstance(reported, int) and reported != slots:
        _emit(f"[NODE] WARNING: llama reports {reported} slot(s) but config asks for {slots} "
              "- the brain clamps routing to the reported value.")

    # Register the process so the SERVER can stop what IT started (stop_all / restart), and
    # publish the model we just launched. An admin CLI run exits right after this and the
    # llama keeps running detached - only a SERVER boot has a live registry to stop it.
    from .process_mgmt import _set_server_process
    from .model_state import key_for_api_url
    from . import model_state as _model_state
    from . import model_persistence
    _set_server_process(proc, key=key_for_api_url(inst.api_url) or (inst.host.lower(), inst.port))
    _model_state.mark_loaded(os.path.basename(model_path),
                             key=key_for_api_url(inst.api_url) or (inst.host.lower(), inst.port))
    model_persistence.write_last_model(os.path.basename(model_path), model_path)

    _print_summary(inst, model_path, log_path)
    return True, True


def _print_summary(inst: LlamaServerInstance, model_path: str, log_path: str) -> None:
    _emit("\n" + "=" * 70)
    _emit(f"LLAMA NODE READY: {inst.name}")
    _emit("=" * 70)
    bind = inst.bind or "127.0.0.1"
    _emit(f"  bind        : {bind}:{inst.port}  (LAN {_lan_ip()})")
    if bind.lower() in ("127.0.0.1", "localhost", "::1"):
        _emit("  note        : loopback - only this machine's brain can reach it. For a remote")
        _emit("                brain set \"bind\": \"0.0.0.0\" and restart.")
    _emit(f"  model       : {os.path.basename(model_path)}")
    _emit(f"  slots       : {inst.concurrency or LLAMA_SERVER_PARALLEL}   "
          f"ctx/slot: {inst.ctx_per_slot or CONTEXT_WINDOW_TOKENS}")
    _emit(f"  log         : {log_path}")
    _emit("  llama-server keeps running after this launcher exits.")
    _emit("=" * 70)


async def _stop_by_port(port: int) -> None:
    from .process_mgmt import _get_process_commandline, _pids_listening_on_port, _kill_llama_server_by_port

    # Verify EVERY owner of the port before killing: _kill_llama_server_by_port() reaches
    # exactly this set, so a non-llama listener sharing the port must block the kill.
    for pid in await asyncio.to_thread(_pids_listening_on_port, port):
        cmdline = await asyncio.to_thread(_get_process_commandline, pid)
        if cmdline is not None and "llama" not in cmdline.lower():
            raise RuntimeError(f"port {port} is PID {pid} ({cmdline[:80]}...) - not a llama-server; refusing to kill it")
    await asyncio.to_thread(_kill_llama_server_by_port, port)
    for _ in range(30):
        if not await _healthy("127.0.0.1", port):
            return
        await asyncio.sleep(0.5)


async def stop(name: str) -> int:
    inst = find(name)
    state = _entry_state(inst.name)
    port = int(state.get("port") or inst.port)

    # "Not healthy" is NOT "not running": a llama still LOADING answers /health with 503.
    # Only declare it stopped when no process listens on the port.
    if not await _healthy(inst.host, port):
        from .process_mgmt import _find_pid_on_port
        pid = await asyncio.to_thread(_find_pid_on_port, port)
        if not pid:
            _write_state(inst.name, desired="stopped", pid=None, port=port)
            _emit(f"[NODE] nothing is listening on port {port} - '{inst.name}' marked as stopped")
            return 0

    _emit(f"[NODE] stopping llama-server on port {port} ('{inst.name}') ...")
    await _stop_by_port(port)
    if await _healthy(inst.host, port):
        _emit(f"[NODE] ERROR: llama-server on port {port} is STILL healthy after the stop "
              f"attempt - not marked stopped. Check it by hand (PID/port).")
        return 1
    _write_state(inst.name, desired="stopped", pid=None, port=port)
    _emit(f"[NODE] stopped. The SERVER will not restart it until you start it again.")
    return 0


async def status(name: str = "") -> int:
    """Print one entry's state (all entries when *name* is empty - the whole file)."""
    entries = [find(name)] if name else all_configs()
    rc = 0
    for inst in entries:
        state = _entry_state(inst.name)
        props = await _fetch_props(inst.host, inst.port)
        healthy = await _healthy(inst.host, inst.port)
        _emit("\n" + "=" * 70)
        _emit(f"LLAMA NODE STATUS: {inst.name}  ({inst.api_url})  kind={inst.kind}")
        _emit("=" * 70)
        _emit(f"  config      : model={inst.model or '(any)'} slots={inst.concurrency or '?'} "
              f"ctx/slot={inst.ctx_per_slot or '?'} threads={inst.threads or '?'} "
              f"gpu_layers={inst.gpu_layers if inst.gpu_layers is not None else '?'}")
        _emit(f"  bind        : {inst.bind or '127.0.0.1'}   auto_start={inst.auto_start}")
        _emit(f"  health      : {'OK' if healthy else 'DOWN'}")
        _emit(f"  desired     : {state.get('desired') or '?'}")
        _emit(f"  pid         : {state.get('pid') or '?'}")
        _emit(f"  log         : {state.get('log') or _log_path(inst.port)}")
        if state.get("last_error"):
            _emit(f"  last error  : {state['last_error']}")
        if props:
            _emit(f"  model       : {props.get('model_path') or props.get('model_alias')}")
            _emit(f"  slots       : {props.get('total_slots')}")
            _emit(f"  ctx/slot    : {_ctx_of(props)}")
            _emit(f"  build       : {props.get('build_info')}")
            if inst.is_local:
                _emit("  brain entry : " + json.dumps(
                    {"name": inst.name, "host": "127.0.0.1", "port": inst.port}))
            else:
                _emit("  brain entry : " + json.dumps(
                    {"name": inst.name, "host": _lan_ip() if inst.host == _lan_ip() else inst.host,
                     "port": inst.port}))
        elif healthy:
            _emit("  props       : UNAVAILABLE - the brain cannot route to this node")
        if not (healthy and props):
            rc = 1
    _emit("=" * 70)
    return rc


def print_cmd(name: str) -> int:
    inst = find(name)
    if not inst.is_local:
        _emit(f"[NODE] '{inst.name}' is a remote endpoint - its command line lives on that machine.")
        return 0
    model_path = _resolve_model(inst)
    _emit(subprocess.list2cmdline(_build_cmd(inst, model_path)))
    return 0


async def restart(name: str) -> int:
    try:
        await stop(name)
    except RuntimeError as e:
        _emit(f"[NODE] stop skipped: {e}")
    inst = find(name)
    _write_state(inst.name, desired="running")
    spawned, ok = await reconcile(inst, allow_override=sys.stdin.isatty(), explicit=True)
    return 0 if ok else 1


def delete(name: str) -> int:
    """Remove an entry from config/llama_servers.json (running node untouched)."""
    inst = find(name)
    entries = [e for e in all_configs() if e.name.lower() != inst.name.lower()]
    if not entries:
        _emit(f"[NODE] REFUSED: '{inst.name}' is the only config - the file must keep at least "
              f"one entry. Edit it instead of deleting it.")
        return 1
    if not _save(entries):
        _emit("[NODE] ERROR: could not write config/llama_servers.json")
        return 1
    _forget_state(inst.name)
    _emit(f"[NODE] removed '{inst.name}' ({inst.host}:{inst.port}) from config/llama_servers.json")
    _emit(f"Remaining configs: {', '.join(e.name for e in entries)}")
    _emit("If its llama-server is still running, stop it with 'stop' before/after this.")
    return 0


async def start_all_local(allow_override: bool = False) -> int:
    """Start every LOCAL entry with auto_start=true, in file order, memory-gated.

    Sequential on purpose: each launch is checked against the memory that is actually free
    after the previous one, so a 3-config fleet either comes up fully or stops at the first
    entry that does not fit - never overloads the machine."""
    failed = 0
    for inst in list_configs():
        if not inst.is_spawnable or not inst.auto_start:
            continue
        _emit(f"\n--- '{inst.name}' ({inst.host}:{inst.port}) ---")
        # 'start all' is an explicit request to run everything: clear any earlier manual
        # stop first, otherwise the stopped-guard would refuse that entry.
        if _entry_state(inst.name).get("desired") == "stopped":
            _write_state(inst.name, desired="running")
        spawned, ok = await reconcile(inst, allow_override=allow_override)
        if not ok:
            failed += 1
            _emit(f"[NODE] '{inst.name}' is NOT up (see the reason above).")
        else:
            _emit(f"[NODE] '{inst.name}' up ({'spawned by us' if spawned else 'attached'})")
    return 1 if failed else 0


def list_summary() -> int:
    """Menu item 1: every config, with what is actually running on its port.

    Shows the WHOLE file (all_configs) - a duplicate host:port is listed too, marked
    SHADOWED, because the runtime fleet keeps only the first entry per address.
    """
    entries = all_configs()
    # shadow_of[name] = name of the EARLIER entry that owns the same host:port.
    owner: dict[tuple, str] = {}
    shadow_of: dict[str, str] = {}
    for inst in entries:
        key = (inst.host.lower(), int(inst.port))
        if key in owner:
            shadow_of[inst.name] = owner[key]
        else:
            owner[key] = inst.name
    _emit("\n" + "=" * 78)
    _emit(f"LLAMA CONFIGS in {_path(os.path.join('config', 'llama_servers.json'))} "
          f"({len(entries)} entry(ies))")
    _emit("=" * 78)

    async def _probe_all():
        return await asyncio.gather(*(_healthy(i.host, i.port) for i in entries))

    results = asyncio.run(_probe_all())
    # Enable ANSI colors on legacy conhost (no-op on Windows Terminal / redirected stdout).
    if os.name == "nt" and sys.stdout.isatty():
        os.system("")

    def _line(text: str) -> None:
        """Console line for the list page; the log gets a color-stripped copy."""
        print(text, flush=True)
        logger.info("[NODE] %s", text.replace("\033[92m", "").replace("\033[0m", ""))

    for i, inst in enumerate(entries, 1):
        healthy = results[i - 1]
        # One line per config: name, address, state and the model FILE NAME only (no path).
        model = inst.model or '(any)'
        if '\\' in model or '/' in model:
            model = os.path.basename(model)
        mark = f"{'RUNNING' if healthy else 'DOWN':<8}"
        if healthy and sys.stdout.isatty():
            mark = "\033[92m" + mark + "\033[0m"
        _line(f"  {i:2d}) {inst.name:<14} {inst.api_url:<24} {mark} model={model}")
        if inst.name in shadow_of:
            _emit(f"      SHADOWED : same host:port as '{shadow_of[inst.name]}' - the runtime "
                  f"keeps only the FIRST entry per address, so this one is not reachable by name")
    _emit("=" * 78)
    # The CLI hints only help when someone types commands directly (scheduled task,
    # plain console). Inside the .bat menu they are noise, so show them only when
    # stdout is NOT an interactive terminal.
    if not sys.stdout.isatty():
        _emit("Configure one:  ZZZ_LLAMA_NODE.bat configure <name>   (or 'add' for a new one)")
        _emit("Start one:      ZZZ_LLAMA_NODE.bat start <name>")
    return 0


# ---------------------------------------------------------------------------
# CLI dispatch (called by utils/start_llama_node/llama_node.py)
# ---------------------------------------------------------------------------

def dispatch(command: str, name: str = "", force: bool = False) -> int:
    """Run one menu command. Returns an exit code; never raises."""
    try:
        if command == "list":
            return list_summary()
        if command == "configure":
            return configure("" if name.lower() in ("", "new", "add") else name)
        if command == "start":
            if name.lower() in ("all", "start-all"):
                # Human at the menu may confirm past the memory gate; a scheduled task
                # (non-TTY) keeps the hard refusal and never hangs on input().
                return asyncio.run(start_all_local(allow_override=sys.stdin.isatty()))
            inst = find(name)
            # An explicit 'start <name>' is a request to run: clear an earlier manual stop
            # (menu item 5) BEFORE reconciling, so the stopped-guard below cannot refuse it.
            if force or _entry_state(inst.name).get("desired") == "stopped":
                _write_state(inst.name, desired="running")
            # explicit=True: a human asked for THIS config to run - auto_start=false must
            # not refuse it; the memory gate below is the only remaining refusal.
            _spawned, ok = asyncio.run(reconcile(inst, allow_override=sys.stdin.isatty(),
                                                 explicit=True))
            return 0 if ok else 1
        if command == "stop":
            return asyncio.run(stop(name))
        if command == "status":
            return asyncio.run(status(name))
        if command == "restart":
            return asyncio.run(restart(name))
        if command == "cmd":
            return print_cmd(name)
        if command == "delete":
            return delete(name)
        if command == "start-all":
            return asyncio.run(start_all_local())
        raise RuntimeError(f"unknown command '{command}'")
    except RuntimeError as e:
        _emit(f"[NODE] ERROR: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        _emit(f"[NODE] ERROR: {type(e).__name__}: {e}")
        return 1
