"""
    COOLEMS CLIENT - Key Module (CLIENT-SIDE)

    Handles:
    - Determining whether a REAL API key is available for outbound SERVER auth
    - Setup-mode detection (nothing configured yet -> only POST /api/auth/set-key works)
    - Authoritative role/email lookup (from the SERVER's last auth_ok, in-memory)

    SECURITY MODEL (2026-10-08): THE KEY LIVES IN THE CLIENT CONFIG FILE.
      * The on-disk file ``CLIENT/config/.api_client_keys.json`` holds exactly ONE
        entry at a time: {"email", "date_acquired", "key"}. The key is stored in
        PLAINTEXT - this is a deliberate, documented trade-off (plain mode): the
        file is per-machine, gitignored and sits next to every other config file.
        NO registry writes anywhere (no setx / HKCU\\Environment) - the previous
        plaintext-in-registry persistence was removed because any local user could
        `reg query` it; a config file in the project folder has no worse exposure
        than the SERVER's own config/.api_keys.json on this same machine.
      * The real key lives in exactly two places:
          1. This CLIENT config file (plaintext, per-machine) - the persistent store
             for headless / non-UI startup and every future process start.
          2. The SERVER's config/.api_keys.json (the authoritative credential store).
        Plus a transient copy in the UI browser (localStorage 'coolems_api_key') -
        the UI injects it into every request/WebSocket via static/ui/api-key.js, so
        ALL user-facing traffic carries the key in headers/query params only.
      * Outbound provider auth frames (CLIENT -> SERVER WebSocket: bootstrap at boot,
        control channel, tool requests) need a key without any UI involvement. Their
        source, in priority order (2026-10-08 file-first):
                a. the runtime key held IN MEMORY for this process lifetime - filled by
                   POST /api/auth/set-key or automatically when the UI presents its
                   localStorage key on an authenticated request (note_presented_key).
                   Authoritative while set: a key entered/changed in the UI must work
                   immediately, even if the config file still holds an OLDER value.
                b. CLIENT/config/.api_client_keys.json ("key" field of the single row) -
                   the persistent per-machine store; written by "Set API Key" (UI) and
                   by the init script. Beats a stale COOLEMS_CLIENT_API_KEY env var, so
                   a key rotation can never be shadowed by an old shell environment.
                c. COOLEMS_CLIENT_API_KEY environment variable - OPTIONAL fallback for
                   headless / CI flows (export it manually if you prefer); the config
                   file takes precedence over it.

    LEGACY MIGRATION: pre-2026-10-08 builds stored {email, date_acquired} only in this
    file (the key lived in the per-user registry via setx). At import time a row that
    carries no "key" field is left as-is - it simply provides NO credential and the
    CLIENT starts in SETUP MODE until the user sets a key in the UI (which writes the
    full row) or exports COOLEMS_CLIENT_API_KEY. Rows from even older builds that carry
    extra confidential fields (role, is_active, max_connections, last_used) are cleaned
    to {email, date_acquired, key?} - those fields were SERVER-side concepts and never
    belong in this file.

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

# Path to CLIENT's local .api_client_keys.json - the per-machine credential store:
# exactly ONE row {"email", "date_acquired", "key"} (plaintext key by design).
_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KEYS_FILE = os.path.join(_CLIENT_DIR, "config", ".api_client_keys.json")

# Optional environment-variable fallback for OUTBOUND provider auth (headless / CI).
# The config file above takes PRECEDENCE over it; the UI flow never needs either -
# the browser injects its localStorage key into every request and that fills the
# runtime store below.
_ENV_KEY_VAR = "COOLEMS_CLIENT_API_KEY"

# In-memory (process lifetime) real key for OUTBOUND provider auth. Never written to
# disk directly. Filled by set_client_api_key() or note_presented_key(); see module
# docstring.
_runtime_key: str = ""

# DEFERRED BOOT FLAG (2026-10-07 setup-mode UI fix): True while the CLIENT's provider/agent
# boot is still running in the BACKGROUND after the UI has already started (setup mode).
# The auth middleware turns this into a 503 "still starting, retry" on keyed requests so a
# request that arrives in the tiny window right AFTER set-key but BEFORE the deferred boot
# finished never reaches routers with provider/agent still None. code_client.py sets it True
# before uvicorn starts and False (finally) when the deferred boot task completes.
_boot_pending: bool = False


def mark_boot_pending(value: bool) -> None:
    """Set/clear the deferred-boot flag (see _boot_pending docstring)."""
    global _boot_pending
    _boot_pending = bool(value)


def is_boot_pending() -> bool:
    """True while the background provider/agent boot has not finished yet."""
    return _boot_pending


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
    """One-time LEGACY CLEANUP (2026-10-08), runs at import time.

    Pre-2026-10-08 builds stored {email, date_acquired} only here (the key lived in
    the per-user registry via setx - that persistence is gone now). Such rows carry
    no credential and are left as-is: they simply do not end setup mode. Rows from
    even older builds may still hold SERVER-side fields (role, is_active,
    max_connections, last_used) - those never belong in this file, so when present
    the row is rewritten atomically to {email, date_acquired, key?}. A clean or
    missing file is left untouched. Never blocks startup; any failure just leaves
    the file as-is (the loaders below handle that gracefully).
    """
    if not os.path.exists(_KEYS_FILE):
        return
    try:
        with open(_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("[CLIENT] Legacy cleanup skipped - file unreadable: %s", e)
        return
    if not isinstance(data, list):
        return

    legacy_fields = ("role", "is_active", "max_connections", "last_used")
    needs_migrate = any(
        isinstance(e, dict) and (any(f in e for f in legacy_fields)) for e in data
    )
    if not needs_migrate:
        return

    cleaned = []
    for e in data:
        if not isinstance(e, dict):
            continue
        row = {"email": e.get("email") or "", "date_acquired": e.get("date_acquired")}
        key = e.get("key")
        if isinstance(key, str) and key.strip():
            row["key"] = key.strip()
        cleaned.append(row)

    try:
        tmp_path = _KEYS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, _KEYS_FILE)  # atomic on Windows & POSIX
        logger.info(
            "[CLIENT] Cleaned legacy fields from %s (now {email, date_acquired, key?} only). "
            "If the row has no 'key' yet, set your API key in UI Settings -> Authentication.",
            _KEYS_FILE,
        )
    except OSError as e:
        logger.error("[CLIENT] Legacy cleanup could not rewrite %s: %s", _KEYS_FILE, e)


# Import-time self-heal (2026-08-23): a fresh checkout gets its bookkeeping file
# created from the example so the user immediately sees where their email/date/key are kept.
_self_heal_keys_file()
# Import-time legacy cleanup (2026-10-08): SERVER-side fields never survive in this file.
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
    """Real key for OUTBOUND provider auth from the environment (optional fallback)."""
    v = os.environ.get(_ENV_KEY_VAR, "")
    return v.strip() if isinstance(v, str) else ""


def _file_api_key() -> str:
    """Real key for OUTBOUND provider auth from CLIENT/config/.api_client_keys.json.

    Reads the "key" field of the single row (plaintext by design - see module
    docstring). Returns "" when the file is missing/unreadable, holds no row with a
    real key, or still carries the example placeholder.
    """
    entries = _read_key_entries()
    if not entries:
        return ""
    for entry in reversed(entries):  # single-row contract; last row wins defensively
        if isinstance(entry, dict):
            key = entry.get("key")
            if isinstance(key, str) and key.strip() and not _is_placeholder_key(key):
                return key.strip()
    return ""


# ===== API Key Loading =====

def load_coolems_api_key() -> str:
    """Return the REAL key used for OUTBOUND CLIENT->SERVER provider auth.

    Source (in priority order, 2026-10-08 file-first):
      1. Runtime in-memory key - set via UI during this process's lifetime
         (POST /api/auth/set-key or note_presented_key). Authoritative for THIS
         process: a key entered/changed in the UI must take effect immediately, even
         when the config file still holds an OLDER value from before the change.
      2. CLIENT/config/.api_client_keys.json ("key" field) - the persistent per-machine
         store; used at boot while no UI session has presented a key yet (then it is
         the only source). Beats a stale COOLEMS_CLIENT_API_KEY env var, so a rotated
         key can never be shadowed by an old shell environment.
      3. COOLEMS_CLIENT_API_KEY environment variable - optional headless/CI fallback.

    Returns "" when no real key is available -> the CLIENT is in SETUP MODE and the
    SERVER simply rejects auth with an actionable message. The user-facing flow never
    depends on this value: the browser injects its localStorage key into every request.
    """
    if _runtime_key:
        return _runtime_key

    file_key = _file_api_key()
    if file_key:
        return file_key

    env_key = _env_api_key()
    if env_key and not _is_placeholder_key(env_key):
        return env_key

    logger.warning(
        "[CLIENT] No API key available for outbound SERVER auth (no key in %s, no %s set, "
        "no UI key presented yet) - SETUP MODE until a key is configured",
        os.path.basename(_KEYS_FILE), _ENV_KEY_VAR,
    )
    return ""


def _is_placeholder_key(key) -> bool:
    """True when a key value is still the example template placeholder."""
    return isinstance(key, str) and key.startswith("PASTE_YOUR_API_KEY")


def get_api_keys() -> set:
    """Return the set of REAL keys currently available to this process.

    Sources: config file (key field) + env var + runtime in-memory key. Used for
    setup-mode detection and basic local gating only - after a rotation the file,
    the env fallback and the runtime store all carry the same current key.
    """
    result = set()
    file_key = _file_api_key()
    if file_key:
        result.add(file_key)
    env_key = _env_api_key()
    if env_key and not _is_placeholder_key(env_key):
        result.add(env_key)
    if _runtime_key:
        result.add(_runtime_key)
    return result


def is_setup_mode() -> bool:
    """True while NO usable outbound credential exists for THIS process (SETUP MODE).

    Setup mode ends when a real key source exists: the runtime in-memory key, the
    config file's "key" field, or the COOLEMS_CLIENT_API_KEY env var. Rows with only
    {email, date_acquired} prove that setup was RUN on some machine at some point -
    they carry no credential. A CLIENT copied to another PC keeps those rows but has
    no key source, and the bootstrap cannot authenticate without one; treating such
    bookkeeping as "configured" made code_client.py take the blocking boot path there
    while the UI (the only place a key can be entered) never started - a deadlock.
    This also matches APIMiddleware._is_local_mode(), which gates on get_api_keys()
    being empty, so both surfaces agree on what setup mode means.
    """
    # The ONLY thing that matters is whether outbound SERVER auth can succeed right
    # now - i.e. whether a real key source exists for this process. Bookkeeping rows
    # (email/date_acquired only) never count; see the docstring above.
    return len(get_api_keys()) == 0


def is_api_key_valid(key: str) -> bool:
    """Check whether *key* matches a real key this CLIENT process knows about.

    STRICT MEMBERSHIP (2026-10-01 hardening): the key must be present in one of this
    process's local sources - runtime in-memory key, config file, or COOLEMS_CLIENT_API_KEY
    env var. When NO source exists yet (clean install), EVERY presented key is rejected:
    that state IS SETUP MODE and both auth surfaces refuse all non-setup traffic before
    reaching this function; the one unlocked endpoint (POST /api/auth/set-key) validates
    length itself and populates runtime + config file BEFORE any other request can run.

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


# ===== Setting API Key Bookkeeping (2026-09-01 SETUP MODE, 2026-10-08 key-in-config) =====

def _sync_outbound_credential_sources(key: str) -> None:
    """Point every outbound-auth source at *key* for this machine.

    Two sources, all in one place so no rotation path can miss one of them (the
    config FILE itself is written by set_client_api_key just before this):
      1. the runtime in-memory store - outbound provider auth for THIS process;
      2. os.environ[COOLEMS_CLIENT_API_KEY] - every env-var reader in this run sees
         the fresh value immediately (covers child processes spawned from this one).
    No registry writes: the persistent per-machine source is CLIENT/config/
    .api_client_keys.json, which works identically on Windows, macOS and Linux.
    """
    global _runtime_key
    _runtime_key = key  # outbound auth follows the freshly presented key
    os.environ[_ENV_KEY_VAR] = key


def set_client_api_key(key: str = "", email: str = "") -> dict:
    """Record {email, date_acquired, key} in .api_client_keys.json and return a result dict.

    Used by POST /api/auth/set-key (loopback-only via APIMiddleware). This is the ONLY
    way to leave SETUP MODE from the UI - before it succeeds, nothing else on the
    CLIENT works at all.

    STORAGE (2026-10-08): the key IS written to this file, in PLAINTEXT (plain mode -
    deliberate trade-off; the file is per-machine and gitignored). It replaces every
    previous row (SINGLE-KEY CONTRACT: one credential per machine) and becomes the
    persistent outbound-auth source for every future headless start. The SERVER's
    config/.api_keys.json remains the authoritative credential store on the server side.

    Behavior:
      * validates the key (non-empty string after trim, 8..256 chars) -> error dict
      * SINGLE-KEY CONTRACT: writes exactly ONE row {email, date_acquired, key},
        replacing every previous row - this machine holds one credential at a time
      * an idempotent re-set for the same email keeps that row's original date_acquired

    Returns:
        {"ok": True,  "message": ...} on success
        {"ok": False, "error":   ...} on validation/write failure
    """
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
        logger.warning("[CLIENT] Key file missing or unparseable - recreating it")
        entries = []

    import datetime as _datetime

    # SINGLE-KEY CONTRACT: exactly ONE entry at a time. Every previous row (placeholder/
    # template rows included) is REPLACED by this record - no pile-up, and never any
    # ambiguity about which credential belongs to this machine. An idempotent re-set for
    # an already-recorded email keeps that row's original date_acquired; a rotation over
    # any other previous state starts the clock fresh.
    existing_date = None
    if email:
        for entry in entries:
            if isinstance(entry, dict) and entry.get("email") == email:
                existing_date = entry.get("date_acquired")
                break

    single = {
        "email": email,  # may be "" when the user sets the key before entering an email
        "date_acquired": existing_date or _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "key": key,      # plaintext by design (2026-10-08) - per-machine config file, gitignored
    }

    if not _write_key_entries([single]):
        return {"ok": False, "error": f"Could not write {os.path.basename(_KEYS_FILE)} - check file permissions."}

    # Explicit UI action overrides the runtime credential (key change from Settings) and
    # keeps this process's env fallback in sync: bootstrap_framework() authenticates to
    # the SERVER BEFORE any browser exists, so it reads load_coolems_api_key(), which now
    # finds the key in the config file above.
    _sync_outbound_credential_sources(key)

    logger.info("[CLIENT] API key saved to %s (plaintext, single row; runtime + env synced)", os.path.basename(_KEYS_FILE))
    return {"ok": True, "message": "API key saved. You can now connect."}


# ===== Role Lookup (authoritative: from the SERVER's auth_ok) =====

def get_key_role(key: str = "") -> Optional[str]:
    """Return the caller's role for logging/display/gating.

    The CLIENT stores NO role in its config file (2026-09-29). The authoritative value is
    the one the SERVER sends in auth_ok after validating the key against its own
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
