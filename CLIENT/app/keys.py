"""
    COOLEMS CLIENT - Key Module (CLIENT-SIDE)

    Handles:
    - Determining whether a REAL API key is available for outbound SERVER auth
    - Setup-mode detection (nothing configured yet -> only POST /api/auth/set-key works)
    - Authoritative role/email lookup (from the SERVER's last auth_ok, in-memory)

    SECURITY MODEL (2026-09-29): NO CONFIDENTIAL DATA ON CLIENT DISK.
      * The on-disk file ``CLIENT/config/.api_client_keys.json`` stores ONLY
        non-confidential bookkeeping per entry: {"email", "date_acquired"}.
        It NEVER contains the API key itself, nor role/is_active/max_connections/
        last_used (those are SERVER-side concepts owned by config/.api_keys.json
        and profiles.json).
      * The real key lives in exactly two places:
          1. The UI browser (localStorage 'coolems_api_key') - the UI injects it
             into every request/WebSocket via static/ui/api-key.js, so ALL
             user-facing traffic carries the key in headers/query params only.
          2. The SERVER's config/.api_keys.json (the authoritative credential store).
      * Outbound provider auth frames (CLIENT -> SERVER WebSocket: bootstrap at boot,
        control channel, tool requests) need a key without any UI involvement. Their
        source, in priority order (2026-09-30 fix - runtime key is authoritative):
                a. the runtime key held IN MEMORY for this process lifetime - filled by
                   POST /api/auth/set-key or automatically when the UI presents its
                   localStorage key on an authenticated request (note_presented_key).
                   Authoritative while set: a key entered/changed in the UI must work
                   immediately, even if COOLEMS_CLIENT_API_KEY still holds an OLDER value
                   from before the change - setx only affects FUTURE processes, so this
                   running client's os.environ stays stale until a restart (see
                   set_client_api_key, which also syncs os.environ for the current run),
                b. COOLEMS_CLIENT_API_KEY environment variable (headless / non-UI flows -
                   used at boot while no UI session has presented a key yet; set it once
                   per machine, e.g. `setx COOLEMS_CLIENT_API_KEY <key>`),
                c. TRANSITIONAL: real keys still sitting in PRE-migration disk files
                   (older builds wrote the key into .api_client_keys.json). Captured at
               import time for this one process run, then stripped from disk.

    LEGACY MIGRATION (2026-09-29): older builds stored the full credential entry here
    (incl. the real key, role, is_active, max_connections, last_used). At import time
    such a file is migrated ONCE: every legacy field is stripped and the file is
    rewritten atomically with only {email, date_acquired}. The captured keys stay in
    memory for this process run (source c above) so an upgraded machine keeps working
    immediately - until COOLEMS_CLIENT_API_KEY is set.

    RENAME (2026-08-23): the file was renamed from ``.api_keys.json`` to
    ``.api_client_keys.json`` so it is not confused with the SERVER's own
    config/.api_keys.json. On import this module self-heals: if the file is
    missing it is seeded from its example template (config/bootstrap.py) so a
    fresh checkout never fails - until a real key exists, load_coolems_api_key()
    returns "" and the CLIENT stays in SETUP MODE with an actionable message.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger("COOLEMS")

# Path to CLIENT's local .api_client_keys.json (NON-CONFIDENTIAL bookkeeping only:
# email + date_acquired per entry - see module docstring).
_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KEYS_FILE = os.path.join(_CLIENT_DIR, "config", ".api_client_keys.json")

# Environment variable holding the REAL key for OUTBOUND provider auth (headless /
# non-UI flows: bootstrap framework fetch at boot, control channel, tool requests).
# The UI flow never needs it - the browser injects its localStorage key into every
# request and that fills the runtime store below.
_ENV_KEY_VAR = "COOLEMS_CLIENT_API_KEY"

# In-memory (process lifetime) real key for OUTBOUND provider auth. Never written to
# disk. Filled by set_client_api_key() or note_presented_key(); see module docstring.
_runtime_key: str = ""

# Transitional legacy keys captured from a PRE-migration file at import time.
_legacy_keys: list = []

# Authoritative identity as reported by the SERVER in auth_ok (in-memory only).
# apply_auth_info() (app/providers/coolems/__init__.py) updates these on EVERY
# successful WebSocket auth - this is the single source of truth for role display.
_server_role: Optional[str] = None
_server_email: Optional[str] = None


def _self_heal_keys_file() -> None:
    """Create a missing .api_client_keys.json from its example template (once).

    Never overwrites an existing file. On any failure it gives up silently -
    the loaders below already handle a missing file gracefully, and
    config/bootstrap.py runs this same seeding at startup for real.
    """
    if os.path.exists(_KEYS_FILE):
        return
    try:
        from config.bootstrap import _seed_from_example
        example = os.path.join(os.path.dirname(_KEYS_FILE), ".api_client_keys.example.json")
        _seed_from_example(_KEYS_FILE, example)
    except Exception as e:  # pragma: no cover - defensive; never break the import
        logger.debug("[CLIENT] Key file self-heal skipped: %s", e)


def _migrate_legacy_file() -> None:
    """One-time LEGACY MIGRATION (2026-09-29), runs at import time.

    Older builds stored the full credential entry in .api_client_keys.json (incl. the
    real key, role, is_active, max_connections, last_used). If such legacy fields are
    present: every captured key goes into _legacy_keys (in-memory, this process run
    only - keeps an upgraded machine working until COOLEMS_CLIENT_API_KEY is set) and
    the file is rewritten atomically with ONLY {email, date_acquired}. A clean or
    missing file is left untouched. Never blocks startup; any failure just leaves the
    file as-is (the loaders below handle that gracefully).
    """
    global _legacy_keys
    if not os.path.exists(_KEYS_FILE):
        return
    try:
        with open(_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("[CLIENT] Legacy migration skipped - file unreadable: %s", e)
        return
    if not isinstance(data, list):
        return

    legacy_fields = ("key", "role", "is_active", "max_connections", "last_used")
    needs_migrate = any(
        isinstance(e, dict) and (any(f in e for f in legacy_fields)) for e in data
    )
    if not needs_migrate:
        return

    cleaned = []
    for e in data:
        if not isinstance(e, dict):
            continue
        key = e.get("key")
        if key and not _is_placeholder_key(key):
            _legacy_keys.append(str(key))  # transitional, memory only (this run)
        cleaned.append({"email": e.get("email") or "", "date_acquired": e.get("date_acquired")})

    try:
        tmp_path = _KEYS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, _KEYS_FILE)  # atomic on Windows & POSIX
        logger.info(
            "[CLIENT] Migrated %s to non-confidential shape (email + date_acquired only); "
            "previously stored key material was removed from disk and kept in memory for THIS run. "
            "Set the COOLEMS_CLIENT_API_KEY environment variable so headless startup keeps working.",
            _KEYS_FILE,
        )
    except OSError as e:
        logger.error("[CLIENT] Legacy migration could not rewrite %s: %s", _KEYS_FILE, e)


# Import-time self-heal (2026-08-23): a fresh checkout gets its bookkeeping file
# created from the example so the user immediately sees where their email/date are kept.
_self_heal_keys_file()
# Import-time legacy migration (2026-09-29): confidential data never survives on disk.
_migrate_legacy_file()


def _read_key_entries():
    """Return the raw list of entries from .api_client_keys.json (None on failure)."""
    if not os.path.exists(_KEYS_FILE):
        return None
    try:
        with open(_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.error("[CLIENT] Failed to parse %s - leaving file untouched", _KEYS_FILE)
        return None
    if not isinstance(data, list):
        logger.error("[CLIENT] %s must contain a JSON array of entries", _KEYS_FILE)
        return None
    return data


def _write_key_entries(entries) -> bool:
    """Atomically write the entry list back to .api_client_keys.json (tmp + rename)."""
    tmp_path = _KEYS_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, _KEYS_FILE)  # atomic on Windows & POSIX
        return True
    except OSError as e:
        logger.error("[CLIENT] Failed to write %s: %s", _KEYS_FILE, e)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def _env_api_key() -> str:
    """Real key for OUTBOUND provider auth from the environment (headless flows)."""
    v = os.environ.get(_ENV_KEY_VAR, "")
    return v.strip() if isinstance(v, str) else ""


# ===== API Key Loading =====

def load_coolems_api_key() -> str:
    """Return the REAL key used for OUTBOUND CLIENT->SERVER provider auth.

    Source (in priority order, 2026-09-30 fix):
      1. Runtime in-memory key - set via UI during this process's lifetime
         (POST /api/auth/set-key or note_presented_key). Authoritative for THIS
         process: a key entered/changed in the UI must take effect immediately, even
         when COOLEMS_CLIENT_API_KEY still holds an OLDER value from before the change.
         That is exactly what setx guarantees NOT to do - it only updates the registry
         (future processes), so this running client's os.environ stays stale until a
         restart; preferring the runtime key removes that "restart required" gap.
      2. COOLEMS_CLIENT_API_KEY environment variable - headless / non-UI flows; used at
         boot while no UI session has presented a key yet (then it is the only source).
      3. Transitional: legacy keys captured from a pre-migration disk file at import

    Returns "" when no real key is available -> the CLIENT is in SETUP MODE and the
    SERVER simply rejects auth with an actionable message. The user-facing flow never
    depends on this value: the browser injects its localStorage key into every request.
    """
    if _runtime_key:
        return _runtime_key

    env_key = _env_api_key()
    if env_key and not _is_placeholder_key(env_key):
        return env_key

    for legacy in _legacy_keys:  # transitional (see module docstring)
        logger.warning(
            "[CLIENT] Using transitional legacy API key from the pre-migration %s - "
            "set the COOLEMS_CLIENT_API_KEY environment variable so headless startup keeps working.",
            os.path.basename(_KEYS_FILE),
        )
        return legacy

    if not env_key:
        logger.warning(
            "[CLIENT] No API key available for outbound SERVER auth (no %s set, no UI key "
            "presented yet) - SETUP MODE until a key is configured", _ENV_KEY_VAR,
        )
    return ""



def _is_placeholder_key(key) -> bool:
    """True when a key value is still the example template placeholder."""
    return isinstance(key, str) and key.startswith("PASTE_YOUR_API_KEY")


def get_api_keys() -> set:
    """Return the set of REAL keys currently available to this process.

    Sources: env var + runtime in-memory key + transitional legacy capture. Used for
    setup-mode detection and basic local gating only - after the 2026-09-29 migration
    NO fresh disk material is ever part of this set (migrated files carry no key field).
    """
    result = set()
    env_key = _env_api_key()
    if env_key and not _is_placeholder_key(env_key):
        result.add(env_key)
    if _runtime_key:
        result.add(_runtime_key)
    result.update(_legacy_keys)  # transitional (see module docstring)
    return result


def is_setup_mode() -> bool:
    """True while the CLIENT has nothing configured yet (SETUP MODE).

    Setup mode ends when ANY of these exists: a real key source (env / runtime /
    legacy) OR bookkeeping recorded by POST /api/auth/set-key (an entry with a real
    email or date_acquired - proves the user ran setup on this machine). The seeded
    placeholder example row counts as "not configured".
    """
    if get_api_keys():
        return False
    entries = _read_key_entries()
    if not entries:
        return True
    for entry in entries:
        if isinstance(entry, dict) and (entry.get("email") or entry.get("date_acquired")):
            return False
    return True


def is_api_key_valid(key: str) -> bool:
    """Check whether *key* matches a real key this CLIENT process knows about.

    STRICT MEMBERSHIP (2026-10-01 hardening): the key must be present in one of this
    process's local sources - runtime in-memory key, COOLEMS_CLIENT_API_KEY env var, or
    transitional legacy capture. When NO source exists yet (clean install), EVERY
    presented key is rejected: that state IS SETUP MODE and both auth surfaces refuse
    all non-setup traffic before reaching this function; the one unlocked endpoint
    (POST /api/auth/set-key) validates length itself and populates runtime + env BEFORE
    any other request can run. The previous fail-open branch ("accept any well-formed
    key when no local source exists") was unreachable from live paths but made the
    contract unsafe for future callers - removed.

    The SERVER remains the authoritative validator of every presented key over
    WebSocket; this function only gates which keys are worth presenting locally.
    """
    if not isinstance(key, str):
        return False
    key = key.strip()
    if not key or _is_placeholder_key(key):
        return False

    return key in get_api_keys()  # fail-closed: unknown keys are never "valid" locally



def note_presented_key(key: str) -> None:
    """Remember a key the UI presented on an authenticated request (in-memory only).

    Called by the auth middleware / WebSocket handshake after is_api_key_valid() passed.
    Fills the runtime store ONLY when it is empty, so outbound provider auth (bootstrap
    already done at boot; later control-channel/tool-request reconnects) uses the key
    that actually belongs to this machine's UI session - without ever touching disk and
    without letting a second browser flip the credential mid-run.
    """
    global _runtime_key
    if not isinstance(key, str):
        return
    key = key.strip()
    if not key or _is_placeholder_key(key) or len(key) > 256:
        return
    if not _runtime_key:
        _runtime_key = key
        logger.info("[CLIENT] Runtime API key captured from UI session (in-memory only - never written to disk)")


# ===== Setting API Key Bookkeeping (2026-09-01 SETUP MODE, 2026-09-29 no-key-on-disk) =====

def set_client_api_key(key: str = "", email: str = "") -> dict:
    """Record {email, date_acquired} in .api_client_keys.json and return a result dict.

    Used by POST /api/auth/set-key (loopback-only via APIMiddleware). This is the ONLY
    way to leave SETUP MODE from the UI - before it succeeds, nothing else on the
    CLIENT works at all.

    SECURITY (2026-09-29): the key itself is NEVER written to disk here. It stays in
    the browser (localStorage) for all user-facing traffic and in the SERVER's
    config/.api_keys.json as the authoritative credential. *key* is accepted so the
    endpoint contract stays compatible; it must be non-empty/valid for setup mode to
    unlock, then it fills the IN-MEMORY runtime store (outbound auth for this process)
    and is discarded - never persisted.

    Behavior:
      * validates the key (non-empty string after trim, 8..256 chars) -> error dict
      * replaces any placeholder/template row instead of piling up rows
      * if a bookkeeping row with the same email already exists it keeps its original
        date_acquired (idempotent re-set from another browser on this machine)

    Returns:
        {"ok": True,  "message": ...} on success
        {"ok": False, "error":   ...} on validation/write failure
    """
    global _runtime_key
    key = (key or "").strip() if isinstance(key, str) else ""
    email = (email or "").strip() if isinstance(email, str) else ""
    if not key:
        return {"ok": False, "error": "API key must be a non-empty string."}
    if len(key) < 8 or len(key) > 256:
        return {"ok": False, "error": f"API key length must be between 8 and 256 characters (got {len(key)})."}

    entries = _read_key_entries()
    if entries is None:
        # Missing/corrupt file: start from a clean list. A corrupt file would otherwise
        # keep the CLIENT in setup mode forever with no way to fix it from the UI.
        logger.warning("[CLIENT] Key bookkeeping file missing or unparseable - recreating it")
        entries = []

    import datetime as _datetime

    # Idempotent: same email already recorded -> the bookkeeping file keeps its
    # original date. (2026-10-05 KEY-DRIFT FIX): this branch must still sync ALL
    # credential sources with the freshly presented key - runtime store, this
    # process's env AND the per-user registry (setx) that every FUTURE headless
    # boot reads via COOLEMS_CLIENT_API_KEY. The old code skipped setx here: a key
    # rotation done in Settings while an email row already existed left the
    # registry holding the OLD key forever, so SERVER rejected bootstrap auth on
    # every restart ("Invalid or inactive API key provided") while the browser UI
    # kept working (its localStorage key is injected per-request). Same best-effort
    # setx as the fresh-row path below.
    if email:
        for entry in entries:
            if isinstance(entry, dict) and entry.get("email") == email:
                _runtime_key = key  # outbound auth follows the freshly presented key
                os.environ[_ENV_KEY_VAR] = key  # (2026-09-30) keep this process's env consistent too
                try:
                    if os.name == "nt":
                        import subprocess as _sp
                        r = _sp.run(["setx", _ENV_KEY_VAR, key], capture_output=True, text=True, timeout=15)
                        if r.returncode == 0:
                            logger.info("[CLIENT] COOLEMS_CLIENT_API_KEY refreshed for future CLIENT starts (new shells)")
                        else:
                            logger.warning("[CLIENT] setx failed (%s) - headless startup keeps using the previous env value", (r.stderr or "").strip()[:120])
                except Exception as e:  # pragma: no cover - best effort only
                    logger.debug("[CLIENT] Could not persist COOLEMS_CLIENT_API_KEY via setx: %s", e)
                return {"ok": True, "message": "API key bookkeeping is already set."}

    # Drop placeholder/template rows so the file holds only real records. A row with
    # an empty email but a date_acquired is a REAL credential ("key set before entering
    # an email"): it survives re-set WITH an email, and when re-setting WITHOUT one it is
    # replaced by the new unlabeled record (documented intent: "replaced, not piled up").
    def _is_stale_row(e):
        """Legacy placeholder row OR an empty bookkeeping row (no email AND no date)."""
        if not isinstance(e, dict):
            return True
        if _is_placeholder_key(e.get("key")):
            return True
        return not (e.get("email") or e.get("date_acquired"))

    cleaned = [e for e in entries if not _is_stale_row(e)]
    if not email:
        cleaned = [e for e in cleaned if isinstance(e, dict) and e.get("email")]
    cleaned.append({
        "email": email,  # may be "" when the user sets the key before entering an email
        "date_acquired": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
    })

    if not _write_key_entries(cleaned):
        return {"ok": False, "error": f"Could not write {os.path.basename(_KEYS_FILE)} - check file permissions."}

    # Explicit UI action overrides the runtime credential (key change from Settings).
    _runtime_key = key

    # Keep headless boot auth in sync: bootstrap_framework() authenticates to the SERVER
    # BEFORE any browser exists, so it reads COOLEMS_CLIENT_API_KEY. Best-effort persist
    # via setx on Windows (per-user registry - outside the repo) so a later CLIENT start
    # uses exactly this key. Non-Windows or failed setx is not fatal: the env var can be
    # exported manually and the UI flow never depends on it.
    try:
        if os.name == "nt":
            import subprocess as _sp
            r = _sp.run(["setx", _ENV_KEY_VAR, key], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                logger.info("[CLIENT] COOLEMS_CLIENT_API_KEY refreshed for future CLIENT starts (new shells)")
            else:
                logger.warning("[CLIENT] setx failed (%s) - headless startup keeps using the previous env value", (r.stderr or "").strip()[:120])
    except Exception as e:  # pragma: no cover - best effort only
        logger.debug("[CLIENT] Could not persist COOLEMS_CLIENT_API_KEY via setx: %s", e)

    # (2026-09-30 fix) sync the CURRENT process's environment too. setx only updates
    # the registry for FUTURE processes - without this line os.environ would keep the
    # OLD value for the whole lifetime of THIS client, so any env-var reader would see
    # stale credentials until a restart (the "first refresh after setting a key is
    # broken" bug). load_coolems_api_key() prefers the runtime key now anyway; this
    # keeps every source consistent within one run.
    os.environ[_ENV_KEY_VAR] = key

    logger.info("[CLIENT] API key bookkeeping set via /api/auth/set-key (setup mode left; key itself stays in browser + SERVER)")
    return {"ok": True, "message": "API key saved. You can now connect."}


# ===== Role Lookup (authoritative: from the SERVER's auth_ok) =====

def get_key_role(key: str = "") -> Optional[str]:
    """Return the caller's role for logging/display/gating.

    The CLIENT stores NO role on disk (2026-09-29). The authoritative value is the
    one the SERVER sends in auth_ok after validating the key against its own
    config/.api_keys.json + profiles.json; apply_auth_info() caches it in-memory on
    every successful WebSocket auth. Before any WS auth has happened yet (e.g. early
    HTTP requests right after startup) there is no server-confirmed role, so this
    falls back to "user" - fail-closed for the local admin gates (run-python etc.).

    REAL permission enforcement happens on SERVER in every case; this value only
    feeds client-side logging and the loopback-only exec gates.
    """
    if _server_role:
        return _server_role
    return "user"  # fallback until the SERVER confirms a role via auth_ok


def get_key_email(key: str = "") -> Optional[str]:
    """Return the caller's email as confirmed by the SERVER (auth_ok), or None."""
    if _server_email:
        return _server_email
    entries = _read_key_entries()
    if entries:
        for entry in reversed(entries):
            email = entry.get("email") if isinstance(entry, dict) else None
            if email:
                return email
    return None


# ===== Subprocess / code-execution permission (SERVER-delivered limitations) =====
#
# The SERVER is the single source of truth for what this CLIENT may execute. Its
# per-profile restrictions arrive over WebSocket in tools_response and are cached by
# RemoteToolOrchestrator at boot (fetched by app/providers/bootstrap.py, installed via
# RemoteToolOrchestrator.install_from_response in app/initialization_client.py):
#   * "python_exec_blocked_libs" - per-profile import blocklist ([] = admin clean set:
#     allow everything; None = no profile set -> built-in defaults apply). Cached in the
#     orchestrator's config constants as PYTHON_EXEC_BLOCKED_LIBS.
#   * "allowed_tools"            - per-role TOOL ALLOWLIST (None = all tools allowed,
#     list = only those tool names are available to this key/profile).
# is_subprocess_allowed() below consults BOTH and fails CLOSED when neither has been
# delivered yet (boot window before the first successful SERVER auth): a CLIENT that
# cannot prove it was told "subprocess is fine" does not spawn child processes.


def _get_tool_orchestrator():
    """Return the process-wide RemoteToolOrchestrator singleton, or None.

    code_client.py declares it as a module global (`tool_orchestrator = None`) and assigns
    it under `if __name__ == "__main__":` only after the SERVER bootstrap succeeds (L149),
    so by the time uvicorn serves any request that global exists on the __main__ module
    - the CLIENT is always run as `python code_client.py`. We read it from
    sys.modules['__main__'] instead of importing 'code_client' by name: that would
    re-execute the entry-point file as a SECOND module object with real side effects
    (setup_logging(), init_db(), double atexit registration). Read-only, no import.

    Before boot completes (or in non-code_client hosts such as tests) this returns
    None - callers then fail CLOSED for exec gates, which is the documented posture.
    """
    try:
        import sys as _sys
        main_mod = _sys.modules.get("__main__")
        if main_mod is not None:
            return getattr(main_mod, "tool_orchestrator", None)
    except Exception:
        pass
    return None


def is_subprocess_allowed() -> tuple[bool, str]:
    """Decide whether this CLIENT may spawn child processes (run-file/run-python).

    The decision uses ONLY limitations delivered by the SERVER - no local role
    verification (the 2026-09-30 role-gate removal stands): whatever profile/role the
    SERVER authenticated this machine as, its restrictions are what apply here.

    Returns:
        (True, "") when execution is allowed, or (False, reason) with a human-readable
        denial message suitable for an HTTP 403 response body.

    Denial rules (first match wins):
      1. SERVER profile blocklist contains "subprocess" -> denied. The per-profile
         python_exec_blocked_libs set is the SERVER's explicit statement that this
         profile must not use subprocess; it applies to every execution surface on the
         CLIENT, HTTP endpoints included. (An EMPTY list = admin clean set = allow all -
         the documented server contract for that value.)
      2. The SERVER's per-role tool ALLOWLIST is a non-None list that contains neither
         "python_exec" nor "subprocess" -> denied: this profile was told it has no
         execution tools at all, so an HTTP exec endpoint must not grant one either.
      3. Neither limitation set has been delivered yet (boot window before the first
         successful SERVER auth) -> fail CLOSED with an actionable message instead of
         guessing "allowed".

    Allow rules: blocklist available and subprocess-free -> allowed; or tool allowlist
    explicitly lists python_exec/subprocess -> allowed. A missing/None field means
    "SERVER did not restrict this dimension", never "SERVER forbade it".
    """
    orch = _get_tool_orchestrator()
    if orch is None:
        return False, ("Subprocess execution is unavailable - the SERVER has not delivered "
                       "this CLIENT's tool configuration yet. Start the COOLEMS SERVER and "
                       "let the CLIENT complete its bootstrap (check the [BOOTSTRAP] log lines), "
                       "then retry.")

    try:
        blocked = orch._config_constants.get("PYTHON_EXEC_BLOCKED_LIBS")  # type: ignore[attr-defined]
    except Exception:
        blocked = None
    if isinstance(blocked, list) and any(isinstance(m, str) and m.strip() == "subprocess" for m in blocked):
        return False, ("Subprocess execution is forbidden by your SERVER-side profile "
                       "(python_exec_blocked_libs contains 'subprocess'). Ask the server "
                       "administrator to adjust this profile's restrictions if you need it.")

    try:
        allowed_tools = orch._cache.get_allowed_tools()  # type: ignore[attr-defined]
    except Exception:
        allowed_tools = None
    if isinstance(allowed_tools, list) and "python_exec" not in allowed_tools and "subprocess" not in allowed_tools:
        return False, ("Subprocess execution is not available for your role - the SERVER's "
                       f"tool allowlist ({', '.join(sorted(str(t) for t in allowed_tools)) or 'empty'}) "
                       "includes neither python_exec nor subprocess. Ask the server administrator to "
                       "grant an execution tool to this profile if you need it.")

    # At least one SERVER-delivered limitation set is present and does not forbid
    # subprocess -> allowed (a None/absent dimension means "not restricted by SERVER").
    return True, ""
