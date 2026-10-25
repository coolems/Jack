"""Config wiring (server <-> client sync)."""

import json
import os
import re
import subprocess
import sys

from .certs import ensure_ui_certs
from .ui import info, line, ok, warn, write_json


def _is_placeholder_key(entry: dict) -> bool:
    key = str(entry.get("key", ""))
    return (not key) or ("PASTE" in key.upper()) or ("EXAMPLE" in key.upper())


def _local_lan_ip() -> str:
    """This machine's LAN IP via the UDP connect trick (stdlib only).

    Returns '' when no private LAN address can be detected - the caller then keeps
    localhost-only and the CLIENT falls back to its own auto-detect on first boot.
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))          # no packet is sent - resolves the interface only
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
            return ip
    except OSError:
        pass
    return ""


def wire_configs(ctx: dict) -> None:
    root = ctx["root"]

    print(); print(line("-")); print("   config wiring (server <-> client sync)"); print(line("-"))

    # ---- 6a. run both official bootstraps so every required file exists ----
    # Each bootstrap runs as its own interpreter with the right PYTHONPATH (the SERVER
    # 'config' package and the CLIENT's 'CLIENT/config' package must not mix in one process).
    for parts, env_root in (
        (("config", "bootstrap.py"), root),
        (("CLIENT", "config", "bootstrap.py"), os.path.join(root, "CLIENT")),
    ):
        rel = "/".join(parts)
        script = os.path.join(root, *parts)
        if not os.path.exists(script):
            warn(f"{rel} missing - skipping its bootstrap")
            continue
        env = dict(os.environ, PYTHONPATH=env_root)
        try:
            r = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
        except OSError as e:                       # exotic systems / AV blocking python - keep going
            warn(f"could not run {rel} ({e}) - continuing; missing files will self-heal at boot")
            continue
        (ok if r.returncode == 0 else warn)(f"ran {rel}" + ("" if r.returncode == 0 else f" (exit {r.returncode})"))

    # ---- 6b. API key into the SERVER file AND the CLIENT config file ----
    # STORAGE (2026-10-08): the CLIENT's .api_client_keys.json holds exactly ONE row
    # {email, date_acquired, key} - the key in PLAINTEXT (plain mode: per-machine,
    # gitignored config file; no registry writes anywhere). The authoritative credential
    # also stays in config/.api_keys.json on this machine; interactive use gets the key
    # from the browser UI (localStorage).
    api_key = ctx["api_key"]
    email = ctx["email"]

    # 6b-i. SERVER file: full credential entry (this is where the real key belongs).
    server_entry = {
        "key": api_key,
        "email": email,
        "role": "admin",
        "date_acquired": None,
        "is_active": True,
        "max_connections": 10,
        "last_used": None,
    }
    server_path = os.path.join(root, "config", ".api_keys.json")
    if not os.path.exists(server_path):       # bootstrap should have created it; be safe
        write_json(server_path, [server_entry])
        ok("config/.api_keys.json: created with new API key")
    else:
        try:
            with open(server_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                entries = []
        except (json.JSONDecodeError, OSError):
            warn("config/.api_keys.json unreadable - rewriting from scratch")
            entries = []
        real = [e for e in entries if isinstance(e, dict) and not _is_placeholder_key(e)]
        changed = False
        kept = [e for e in entries if isinstance(e, dict) and (not _is_placeholder_key(e))]
        if len(kept) != len(entries):
            changed = True
        if not any(isinstance(e, dict) and e.get("key") == api_key for e in kept):
            kept.append(server_entry)
            changed = True
        if changed:
            write_json(server_path, kept)
            ok("config/.api_keys.json: API key installed" + (f" (+{len(real)} existing real key(s) kept)" if real else ""))
        else:
            ok("config/.api_keys.json: already in sync - untouched")

    # 6b-ii. CLIENT file: the per-machine credential row {email, date_acquired, key}
    # (key in PLAINTEXT by design - no registry writes; works on Windows/macOS/Linux).
    client_path = os.path.join(root, "CLIENT", "config", ".api_client_keys.json")
    if not os.path.exists(client_path):       # bootstrap should have created it; be safe
        write_json(client_path, [{"email": email, "date_acquired": None, "key": api_key}])
        ok("CLIENT/config/.api_client_keys.json: created with API key (plaintext, single row)")
    else:
        try:
            with open(client_path, "r", encoding="utf-8") as f:
                c_entries = json.load(f)
            if not isinstance(c_entries, list):
                c_entries = []
        except (json.JSONDecodeError, OSError):
            warn("CLIENT/config/.api_client_keys.json unreadable - rewriting from scratch")
            c_entries = []
        # SINGLE-KEY CONTRACT: exactly ONE row {email, date_acquired, key}. Every previous
        # row is replaced (placeholder rows from the example template included); a matching
        # email keeps its original date_acquired.
        existing_date = None
        for e in c_entries:
            if isinstance(e, dict) and (e.get("email") or "") == email:
                existing_date = e.get("date_acquired")
                break
        write_json(client_path, [{"email": email, "date_acquired": existing_date, "key": api_key}])
        ok("CLIENT/config/.api_client_keys.json: API key installed (plaintext, single row)")

    # 6b-iii. This process's env fallback gets the key too (child processes spawned from
    # init inherit it). The persistent per-machine source is the CLIENT config file above -
    # no registry writes anywhere.
    os.environ["COOLEMS_CLIENT_API_KEY"] = api_key

    # ---- 6c. profiles.json -> this machine's real folders ----
    prof_path = os.path.join(root, "config", "profiles.json")
    if os.path.exists(prof_path):
        try:
            with open(prof_path, "r", encoding="utf-8") as f:
                profiles = json.load(f)
            if not isinstance(profiles, dict):
                raise ValueError("not a dict")
        except (json.JSONDecodeError, OSError, ValueError) as e:
            warn(f"profiles.json unreadable ({e}) - leaving it for manual fix; model folder is "
                 f"{ctx['model_folder']}")
            profiles = None
    else:
        profiles = None
        warn(f"profiles.json not found (bootstrap should create it) - model folder is {ctx['model_folder']}")

    if isinstance(profiles, dict):
        changed_roles = []
        missing_slots = []
        for role, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            _raw_models = profile.get("allowed_models")
            before = (
                list(_raw_models) if isinstance(_raw_models, list) else _raw_models,
                profile.get("allowed_models_folders"),
                profile.get("ocr_model"),
                profile.get("max_parallel_slots"),
            )
            # (2026-10 fleet) Model authorization is by FILENAME; folders are gone.
            profile.pop("allowed_models_folders", None)
            existing_models = profile.get("allowed_models")
            if existing_models is None:
                # Admin ("all models") stays all - do not turn it into a one-model list.
                pass
            elif isinstance(existing_models, list):
                if ctx["final_model_name"] not in existing_models:
                    existing_models.append(ctx["final_model_name"])
                profile["allowed_models"] = existing_models
            else:
                profile["allowed_models"] = [ctx["final_model_name"]]

            if ctx.get("ocr_folder"):
                profile["ocr_model"] = ctx["ocr_folder"]
            else:
                # OCR declined - clear stale paths from other machines (consumers skip empty).
                profile["ocr_model"] = ""

            slots = profile.get("max_parallel_slots")
            if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
                missing_slots.append(role)

            after = (
                profile.get("allowed_models"),
                profile.get("allowed_models_folders"),
                profile.get("ocr_model"),
                profile.get("max_parallel_slots"),
            )
            if before != after:
                changed_roles.append(role)
        if changed_roles:
            write_json(prof_path, profiles)
            ok(f"profiles.json: model/OCR paths rewired for {len(changed_roles)} profile(s): "
               + ", ".join(sorted(changed_roles)))
        else:
            ok("profiles.json: already pointing at the right model - untouched")
        if missing_slots:
            warn(
                "profiles.json: role(s) without a valid max_parallel_slots: "
                + ", ".join(sorted(missing_slots))
                + " - authentication for them will be DENIED until you add it "
                  "(e.g. \"max_parallel_slots\": 1)."
            )

    # ---- 6d. pre-seed last-loaded model so SERVER boots straight into it ----
    last_path = os.path.join(root, "config", ".last_model_loaded.json")
    payload = {"model": ctx["final_model_name"], "path": ctx["model_path"]}
    current = None
    if os.path.exists(last_path):
        try:
            with open(last_path, "r", encoding="utf-8") as f:
                current = json.load(f)
        except (json.JSONDecodeError, OSError):
            current = None      # corrupt/unreadable - just overwrite it below
    if current != payload:
        write_json(last_path, payload)
        ok(f".last_model_loaded.json: SERVER will boot into {ctx['final_model_name']}")

    # ---- 6e. patch CONTEXT_WINDOW_TOKENS to the tier value ----
    cfg_py = os.path.join(root, "config", "config.py")
    want_ctx = ctx["tier"]["ctx"]
    try:
        with open(cfg_py, "r", encoding="utf-8") as f:
            src = f.read()
        new_src, n = re.subn(r"(CONTEXT_WINDOW_TOKENS:\s*int\s*=\s*)\d+", rf"\g<1>{want_ctx}", src, count=1)
        if n == 0:
            warn("Could not find CONTEXT_WINDOW_TOKENS in config/config.py - left as-is")
        elif new_src != src:
            with open(cfg_py, "w", encoding="utf-8") as f:
                f.write(new_src)
            ok(f"config/config.py: CONTEXT_WINDOW_TOKENS -> {want_ctx} (tier {ctx['tier']['id']})")
        else:
            ok(f"config/config.py: CONTEXT_WINDOW_TOKENS already {want_ctx} - untouched")
    except OSError as e:
        warn(f"Could not patch config/config.py: {e}")

    # ---- 6f. CLIENT settings: seed the failover list so the CLIENT folder is portable ----
    # The CLIENT tries server_addresses in order (localhost first, then this machine's LAN IP),
    # so the same CLIENT\ folder works on THIS PC and - when copied to any other LAN PC - there,
    # with no address editing. No local-vs-remote question needed at init time.
    client_cfg_dir = os.path.join(root, "CLIENT", "config")
    settings_path = os.path.join(client_cfg_dir, "settings.json")
    data = {}
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (json.JSONDecodeError, OSError):
        pass
    local_addr = "localhost:8080"          # repo convention: index 0 of the failover list
    lan_ip = _local_lan_ip()
    seeded = [local_addr] + ([f"{lan_ip}:8080"] if lan_ip else [])
    addrs = [a for a in (data.get("server_addresses") or []) if isinstance(a, str) and a.strip()]
    changed_client = False
    for a in reversed(seeded):             # insert newest first so seeded order ends up local-first
        if a not in addrs:
            addrs.insert(0, a)
            changed_client = True
    data["server_addresses"] = addrs
    # Drop a stale singular 'server_address' (e.g. another machine's IP left over from an old
    # clone): the ordered list is the single source of truth now; a known-good value gets
    # re-recorded by the CLIENT itself on next successful connect.
    if str(data.get("server_address") or "").strip() not in addrs:
        data.pop("server_address", None)
        changed_client = True
    if changed_client:
        write_json(settings_path, data)
        ok(f"CLIENT settings.json: server_addresses seeded -> {', '.join(addrs)}")
        ok("   (the CLIENT\\ folder is now portable - copy it to any other LAN PC and run "
           "ZZZ_CLIENT.bat there; it finds this SERVER automatically)")
    else:
        ok(f"CLIENT settings.json: already in sync ({', '.join(addrs)}) - untouched")

    # ---- 6g. CLIENT UI TLS certificates (browser trust for the local UI URL, port from config.CLIENT_UI_PORT) ----
    ctx["ui_certs_ok"] = ensure_ui_certs(root)

    # ---- 6h. default LOCAL llama node config so ZZZ_SERVER.bat can auto-start it ----
    write_local_node_config(ctx)


def _read_int_from_config_py(cfg_py: str, name: str, fallback: int) -> int:
    """Read an integer constant from config/config.py (init is stdlib-only - no import)."""
    try:
        with open(cfg_py, "r", encoding="utf-8") as f:
            src = f.read()
        m = re.search(rf"^{re.escape(name)}\s*[:=]\s*(?:int\s*=\s*)?(\d+)", src, re.M)
        if m:
            return int(m.group(1))
    except OSError:
        pass
    return fallback


def write_local_node_config(ctx: dict) -> None:
    """Write DEFAULT settings for the local llama node (2026-10-25 auto-start).

    config/llama_node.json is the single file both paths use: init writes sane defaults
    here, and the admin wizard (utils/start_llama_node) overwrites it when an admin wants
    a custom node. At boot the SERVER reconciles each LOCAL instance against this file:
    healthy matching node -> attach; nothing running -> spawn detached with these settings
    ("auto_start": true). NEVER overwrites an existing file - an admin's configuration wins.
    """
    import socket

    root = ctx["root"]
    path = os.path.join(root, "config", "llama_node.json")
    if os.path.exists(path):
        ok("config/llama_node.json: already present (admin/custom) - kept as-is")
        return

    cfg_py = os.path.join(root, "config", "config.py")
    port = _read_int_from_config_py(cfg_py, "LLAMA_SERVER_PORT", 5000)
    threads = _read_int_from_config_py(cfg_py, "LLAMA_SERVER_THREADS", 8)
    gpu_layers = _read_int_from_config_py(cfg_py, "LLAMA_SERVER_GPU_LAYERS", 99)

    # Concurrency by GPU tier: only a >=28 GB card (tier A) comfortably runs 2 slots with
    # full per-slot context; everything else stays at the stable single slot.
    concurrency = 2 if ctx["tier"]["id"] == "A" else 1

    cfg = {
        "name": socket.gethostname(),
        "model": ctx["model_path"],
        "bind": "127.0.0.1",
        "port": port,
        "concurrency": concurrency,
        "ctx_per_slot": int(ctx["tier"]["ctx"]),
        "threads": threads,
        "gpu_layers": gpu_layers,
        "extra_args": [],
        # true = ZZZ_SERVER.bat boot spawns this node when nothing is on its port;
        # set false to make the SERVER probe/attach only (admin starts it by hand).
        "auto_start": True,
    }
    try:
        write_json(path, cfg)
        ok(f"config/llama_node.json: default local node written "
           f"(port {port}, {concurrency} slot(s), ctx/slot {cfg['ctx_per_slot']}, auto_start)")
        info("   ZZZ_SERVER.bat will now start this node automatically when nothing is on its port.")
    except OSError as e:
        warn(f"Could not write config/llama_node.json ({e}) - the SERVER falls back to "
             f"default spawn settings at boot")
