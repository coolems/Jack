"""Config wiring (server <-> client sync)."""

import json
import os
import re
import subprocess
import sys
import time

from .certs import ensure_ui_certs
from .ui import backup_once, info, line, ok, warn, write_json


def _is_placeholder_key(entry: dict) -> bool:
    key = str(entry.get("key", ""))
    return (not key) or ("PASTE" in key.upper()) or ("EXAMPLE" in key.upper())


def wire_configs(ctx: dict) -> None:
    root = ctx["root"]
    stamp = time.strftime("%Y%m%d_%H%M%S")

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
            warn("config/.api_keys.json unreadable - backing up and rewriting")
            backup_once(server_path, stamp)
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
            backup_once(server_path, stamp)
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
            warn("CLIENT/config/.api_client_keys.json unreadable - backing up and rewriting")
            backup_once(client_path, stamp)
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
        for role, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            before = (profile.get("allowed_models_folders"), profile.get("ocr_model"))
            profile["allowed_models_folders"] = [ctx["model_folder"]]
            if ctx.get("ocr_folder"):
                profile["ocr_model"] = ctx["ocr_folder"]
            else:
                # OCR declined - clear stale paths from other machines (consumers skip empty).
                profile["ocr_model"] = ""
            after = (profile["allowed_models_folders"], profile.get("ocr_model"))
            if before != after:
                changed_roles.append(role)
        if changed_roles:
            backup_once(prof_path, stamp)
            write_json(prof_path, profiles)
            ok(f"profiles.json: model/OCR paths rewired for {len(changed_roles)} profile(s): "
               + ", ".join(sorted(changed_roles)))
        else:
            ok("profiles.json: already pointing at the right folders - untouched")

    # ---- 6d. pre-seed last-loaded model so SERVER boots straight into it ----
    last_path = os.path.join(root, "config", ".last_model_loaded.json")
    payload = {"model": ctx["final_model_name"], "path": ctx["model_path"]}
    current = None
    if os.path.exists(last_path):
        try:
            with open(last_path, "r", encoding="utf-8") as f:
                current = json.load(f)
        except (json.JSONDecodeError, OSError):
            backup_once(last_path, stamp)
    if current != payload:
        if os.path.exists(last_path):
            backup_once(last_path, stamp)
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
            backup_once(cfg_py, stamp)
            with open(cfg_py, "w", encoding="utf-8") as f:
                f.write(new_src)
            ok(f"config/config.py: CONTEXT_WINDOW_TOKENS -> {want_ctx} (tier {ctx['tier']['id']})")
        else:
            ok(f"config/config.py: CONTEXT_WINDOW_TOKENS already {want_ctx} - untouched")
    except OSError as e:
        warn(f"Could not patch config/config.py: {e}")

    # ---- 6f. CLIENT settings + working root ----
    client_cfg_dir = os.path.join(root, "CLIENT", "config")
    settings_path = os.path.join(client_cfg_dir, "settings.json")
    if ctx["same_machine_client"]:
        data = {}
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            pass
        local_addr = "localhost:8080"          # repo convention: index 0 of the failover list
        addrs = [a for a in (data.get("server_addresses") or []) if isinstance(a, str) and a.strip()]
        changed_client = False
        if local_addr not in addrs:
            addrs.insert(0, local_addr)
            changed_client = True
        data["server_addresses"] = addrs
        if data.get("server_address") != local_addr:
            data["server_address"] = local_addr
            changed_client = True
        if changed_client:
            backup_once(settings_path, stamp)
            write_json(settings_path, data)
            ok(f"CLIENT settings.json: {local_addr} set as primary server address (same machine)")
        else:
            ok("CLIENT settings.json: already pointing at localhost - untouched")
    else:
        info("CLIENT will run on another PC - leaving its settings.json for auto-detect "
             "(it finds the SERVER's LAN IP on first boot; make sure both PCs are on the same network).")


    # ---- 6g. CLIENT UI TLS certificates (browser trust for the local UI URL, port from config.CLIENT_UI_PORT) ----
    ctx["ui_certs_ok"] = ensure_ui_certs(root)
