"""
COOLEMS CLIENT configuration.
All client-side values set here. NO server secrets (no API keys, no auth profiles).

CRITICAL: CLIENT ONLY supports 'coolems_client' provider.
All requests MUST go through the SERVER via WebSocket relay.
Direct LLM access is BLOCKED in CLIENT code.

Remote PC Setup:
  Configure via the Settings UI (gear icon) -> Server Address field.
  This writes directly to settings.json which is the SINGLE SOURCE OF TRUTH.

For server config with secrets, see root config/config.py (NEVER included in CLIENT distribution).
"""

import logging
import os
from typing import Optional


# --- Context / Token Limits ---
CONTEXT_WINDOW_TOKENS: int | None = None  # Set dynamically from SERVER during auth handshake - NO fallback
HISTORY_CONTEXT_FRACTION: float = 0.90
CHARS_PER_TOKEN: float = 4.0
SYSTEM_PROMPT_TOKENS_ESTIMATE: int = 2000
IMAGE_TOKENS_PER_512PX: int = 4000

# --- Cross-Task Memory ---
CROSS_TASK_MEMORY_CONVERSATIONS: int = 5
CROSS_TASK_MEMORY_CONTEXT_FRACTION: float = 0.15

# --- Provider / Model (Client Side) ---
PROVIDER: str = "coolems_client"
# --- Default Role for API keys (used by tool_executor) ---
DEFAULT_ROLE_NO_KEY: str = "user"

# --- Local CLIENT Web UI Port ---
# HTTP port the CLIENT web UI binds to. entry/cli_parser.py uses this as its --port
# default and code_client.py passes it through create_app() / uvicorn. The SERVER root
# keeps a mirror copy (config/config.py CLIENT_UI_PORT) because code.py launches this
# process with the same port - change ONE, change the other.
CLIENT_UI_PORT: int = 8000


# ============================================================
# Settings Persistence - MUST be defined BEFORE anything uses it
# settings.json is the SINGLE SOURCE OF TRUTH for server_address
# ============================================================

_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def _load_settings() -> dict:
    """Load settings from settings.json. Returns empty dict on failure."""
    import json
    try:
        if os.path.exists(_SETTINGS_FILE):
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logging.getLogger("COOLEMS").debug("Failed to load settings file - returning empty dict")
    return {}


def _save_settings(settings: dict) -> None:
    """Save settings dict to settings.json."""
    import json
    try:
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        import logging
        logging.getLogger("COOLEMS").warning(f"Failed to save settings: {e}")


# ============================================================
# Search Engines Persistence - search_engines.json is the SINGLE SOURCE OF TRUTH
# for which web search engines are enabled and their per-engine settings.
# The Settings UI reads/writes it through /api/search-engines (app/routers/agent.py);
# tools/web_tools/web_search.py reads the same file live on every search, so saves
# apply immediately - no restart needed. There are NO fallback engine lists anywhere:
# a missing or corrupt file means "no engines" and the user is told which file to fix.
# ============================================================

_SEARCH_ENGINES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_engines.json")


def _validate_engine_entry(engine) -> str | None:
    """Validate one search-engine entry (from the UI). Returns an error string or None."""
    import re as _re

    if not isinstance(engine, dict):
        return "Each engine must be a JSON object."
    eid = engine.get("id")
    if not isinstance(eid, str) or not _re.fullmatch(r"[a-z0-9_]{1,40}", eid):
        return f"Engine id {eid!r} is invalid (must match [a-z0-9_] 1-40 chars)."
    name = engine.get("name")
    if not isinstance(name, str) or not name.strip():
        return f"Engine '{eid}' has an empty name."
    if not isinstance(engine.get("enabled"), bool):
        return f"Engine '{eid}': 'enabled' must be true/false."
    priority = engine.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool) or priority < 1:
        return f"Engine '{eid}': 'priority' must be an integer >= 1."
    settings = engine.get("settings", {})
    if not isinstance(settings, dict):
        return f"Engine '{eid}': 'settings' must be a JSON object."
    for key, value in settings.items():
        if isinstance(value, bool) or isinstance(value, (int, float)) or isinstance(value, str):
            continue
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            continue
        return f"Engine '{eid}': setting '{key}' must be a number, string, boolean or list of strings."
    return None


def load_search_engines() -> list:
    """Read the engine list from search_engines.json (fresh on every call).

    Returns data["engines"] as-is. On missing/corrupt file: logs an error and
    returns [] - NO fallback engines are invented anywhere in the codebase.
    """
    import json
    try:
        with open(_SEARCH_ENGINES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        engines = data.get("engines") if isinstance(data, dict) else None
        if not isinstance(engines, list):
            raise ValueError("'engines' must be a list")
        return [e for e in engines if isinstance(e, dict)]
    except Exception as e:
        logging.getLogger("COOLEMS").error(
            f"search_engines.json missing or invalid ({e}) - no search engines available. "
            f"Fix {_SEARCH_ENGINES_FILE}"
        )
        return []


def save_search_engines(engines) -> None:
    """Validate and write the engine list back to search_engines.json (pretty-printed).

    Raises ValueError with a human-readable message when validation fails -
    the file is left untouched in that case.
    """
    import json

    if not isinstance(engines, list) or not engines:
        raise ValueError("Engine list must be a non-empty array.")
    seen = set()
    for e in engines:
        err = _validate_engine_entry(e)
        if err:
            raise ValueError(err)
        if e["id"] in seen:
            raise ValueError(f"Duplicate engine id '{e['id']}'.")
        seen.add(e["id"])
    with open(_SEARCH_ENGINES_FILE, "w", encoding="utf-8") as f:
        json.dump({"engines": engines}, f, indent=2)


# --- Coolems Client Connection Settings ---
# settings.json is the SINGLE SOURCE OF TRUTH for server address.
# No env var overrides. No crappy fallbacks.

_COOLEMS_DEFAULT_PORT: int = 8080


def _get_local_lan_ip() -> str:
    """Auto-detect the primary LAN IP address of this machine.

    Returns an IP like '192.168.x.x' or '10.x.x.x'. Falls back to 'localhost'.
    Uses a UDP socket trick to find the outgoing interface IP without actually connecting.
    """
    import socket
    try:
        # Create a UDP socket and connect to a dummy address to discover local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # This doesn't actually send anything - just resolves the local interface
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()

        # Validate it's a private LAN IP, not loopback
        if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
            return ip
    except Exception:
        pass

    # Fallback: try iterating interfaces (more reliable on some systems)
    try:
        import psutil
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    ip = addr.address
                    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                        return ip
    except Exception:
        pass

    # Ultimate fallback - only used when all detection methods fail
    import logging
    logging.getLogger("COOLEMS").warning(
        "Could not auto-detect LAN IP - falling back to localhost."
    )
    return "localhost"


def _normalize_server_addresses(raw) -> list[str]:
    """Normalize raw settings.json address value(s) into an ordered, de-duplicated list."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[str] = []
    for item in items:
        s = str(item).strip()
        if not s or s.lower() in ("none", "null"):
            continue
        if s not in out:
            out.append(s)
    return out


def get_server_address_list() -> list[str]:
    """Fresh read of the ordered SERVER address failover list from settings.json.

    Order = try order: index 0 is tried FIRST (put 'localhost:8080' there so local
    always wins), and each next address is only attempted when the previous one is
    unreachable. This makes the CLIENT folder portable across machines - ship it with
    ["localhost:8080", "<LAN-IP>:8080"] and it works on the SERVER machine itself AND
    on any other LAN machine without edits.

    Both settings.json shapes are accepted:
      "server_addresses": ["localhost:8080", "192.168.1.50:8080"]    (preferred)
      "server_address":   "localhost:8080"                            (legacy single)
    When both are present the list wins; a legacy singular value not already in the
    list is appended at the end.
    """
    settings = _load_settings()
    addrs = _normalize_server_addresses(settings.get("server_addresses"))
    for a in _normalize_server_addresses(settings.get("server_address")):
        if a not in addrs:
            addrs.append(a)
    return addrs


def get_good_server() -> str | None:
    """Return the last CONFIRMED-GOOD server address, or None.

    (2026-09-01 sticky failover) The coolems protocol layer remembers which address
    actually answered a connection and persists it here under the legacy singular
    'server_address' key - that key is already merged into get_server_address_list()
    by every reader, so nothing else needs to change to consume this value.
    """
    try:
        settings = _load_settings()
    except Exception:
        return None
    good = str(settings.get("server_address") or "").strip()
    if not good:
        return None
    # Only trust it when it is still in the CONFIGURED list ('server_addresses').
    # get_server_address_list() would also accept a lone legacy value, which must
    # NOT happen here: a persisted address removed from 'server_addresses' by an
    # edit is stale and has to be ignored, not hijack the connection order.
    if good in _normalize_server_addresses(settings.get("server_addresses")):
        return good
    return None


def _remember_good_server(addr: str) -> None:
    """Persist *addr* as the known-good server (sticky failover, 2026-09-01).

    Written to settings.json under 'server_address' so it survives CLIENT restarts:
    after a reboot the client goes straight to the address that worked last time and
    only scans the rest of the list if THAT one is dead. No-op on failure - memory-only
    stickiness (in-process) still works without the file write.
    """
    import logging
    try:
        addr = str(addr).strip()
        if not addr or addr.lower() in ("none", "null"):
            return
        settings = _load_settings()
        if str(settings.get("server_address") or "").strip() == addr:
            return  # already recorded - do not rewrite the file every connect
        settings["server_address"] = addr
        _save_settings(settings)
        logging.getLogger("COOLEMS").info(f"[CLIENT] Remembered good server address: {addr}")
    except Exception as e:
        logging.getLogger("COOLEMS").debug(f"Could not persist good server address: {e}")


def _resolve_client_server_address() -> str:
    """Resolve the PRIMARY server address from settings.json (SINGLE SOURCE OF TRUTH).

    Returns the known-good address when one is recorded and still in the list, else
    the FIRST entry of get_server_address_list(). The full ordered failover list is
    read fresh by every connection via get_server_address_list(); this function exists
    for boot-time caching and UI display.

    Logic:
      1. Read settings.json (server_addresses list, legacy server_address fallback).
      2. If empty/missing -> auto-detect local LAN IP, WRITE it to settings.json, return it.
      3. No env var overrides. No crappy fallbacks. settings.json is THE source of truth.

    Called once at module import time to set COOLEMS_CLIENT_SERVER_ADDRESS.
    Also callable at runtime via _resolve_client_server_address() for fresh reads.
    """
    import logging
    logger = logging.getLogger("COOLEMS")

    # Read from settings.json (the single source of truth)
    try:
        addrs = get_server_address_list()
        if addrs:
            # (2026-09-01 sticky failover) when a known-good address is recorded and
            # still in the list, it becomes the PRIMARY boot/display value - the same
            # one _get_ws_candidates() will put at index 0 of every connection attempt.
            good = get_good_server()
            primary = good if good else addrs[0]
            order = f" (failover order: {', '.join(addrs)})" if len(addrs) > 1 else ""
            logger.info(f"Using server address from settings.json: {primary}{order}")
            return primary
    except Exception as e:
        logger.warning(f"Failed to read settings.json: {e}")

    # FIRST BOOT: No saved address -> auto-detect and WRITE it to settings.json immediately
    lan_ip = _get_local_lan_ip()
    default_addr = f"{lan_ip}:{_COOLEMS_DEFAULT_PORT}"

    # Write it to settings.json so it becomes the permanent source of truth
    try:
        new_settings = {"server_address": default_addr}
        # Preserve any other existing keys in the file
        try:
            existing = _load_settings()
            if existing:
                new_settings.update(existing)
                new_settings["server_address"] = default_addr  # Ensure our value wins
        except Exception:
            pass
        _save_settings(new_settings)
        logger.info(
            f"[FIRST BOOT] No server_address in settings.json - auto-detected and saved: {default_addr}"
        )
    except Exception as e:
        logger.warning(f"Could not write default address to settings.json: {e}")

    return default_addr




# Module-level cached value (set once at import time).
# Requires CLIENT restart after UI changes - this is by design.
COOLEMS_CLIENT_SERVER_ADDRESS: str = _resolve_client_server_address()


# --- Web Relay UI availability switch (2026-09-23) ---
# WEB_RELAY_UI_ENABLED controls whether the "Internet Web Relay" option is visible
# in the Settings UI at all:
#   True  -> Connection Mode menu shows both Direct and Internet Web Relay (default).
#   False -> the whole Connection Mode group is hidden from the UI, AND the client
#            behaves as pure Direct mode: get_connection_mode() returns 'direct' no
#            matter what settings.json or COOLEMS_CONNECTION_MODE say, so a stale
#            saved relay configuration can never activate silently.
# Requires CLIENT restart after changing (read at import time).
# This code is still under development. Please do not use it yet.
WEB_RELAY_UI_ENABLED: bool = False


def get_connection_mode() -> str:
    """'direct' | 'web_relay' — how the CLIENT talks to the SERVER (2026-09-23).

    settings.json is the SINGLE SOURCE OF TRUTH ('connection_mode', written by the
    Settings UI via POST /api/settings); env vars override for non-UI deploys.
    DEFAULT IS DIRECT: a missing/unknown value always means direct LAN mode, so an
    untouched CLIENT behaves exactly like before this option existed.
    """
    if not WEB_RELAY_UI_ENABLED:
        return "direct"  # hard kill switch - relay option disabled by CLIENT config

    env = os.environ.get("COOLEMS_CONNECTION_MODE", "").strip().lower()
    if env in ("direct", "web_relay"):
        return env
    try:
        v = str(_load_settings().get("connection_mode") or "").strip().lower()
    except Exception:
        v = ""
    return v if v in ("direct", "web_relay") else "direct"


def get_web_relay_address() -> Optional[str]:
    """Relay address 'host[:port]' from settings.json, or None when not configured.

    Settings UI fields relay_host + relay_port are the source of truth; env vars
    COOLEMS_RELAY_HOST / COOLEMS_RELAY_PORT override for non-UI deploys. A bare host
    (no port) is valid - WEB_RELAY_DEFAULT_PORT applies at connect time.
    """
    host = os.environ.get("COOLEMS_RELAY_HOST", "").strip() or \
        str(_load_settings().get("relay_host") or "").strip()
    if not host:
        return None
    port_env = os.environ.get("COOLEMS_RELAY_PORT", "").strip()
    if port_env.isdigit():
        port = int(port_env)
    else:
        try:
            port = int(str(_load_settings().get("relay_port") or "").strip())
        except (TypeError, ValueError):
            port = 0
    return f"{host}:{port}" if port and 1 <= port <= 65535 else host


# --- Runtime behavior settings (2026-10-02) -----------------------------------
# The Settings UI "Runtime" tab stores these in settings.json via POST /api/settings:
#   python_exec_auto_approve            : "manual" | "auto"  (default "manual")
#       manual -> the python_exec approval dialog waits until the user decides
#                 (the original behavior - there is NO timeout).
#       auto   -> the dialog still opens, but after python_exec_auto_approve_seconds
#                 it approves itself and the code runs without a click.
#   python_exec_auto_approve_seconds    : int 1..3600 (default 5)
# Both are read LIVE from settings.json on every access so a save in the UI applies
# to the very next dialog - no restart needed.

PYTHON_EXEC_AUTO_APPROVE_DEFAULT = "manual"
PYTHON_EXEC_AUTO_APPROVE_SECONDS_DEFAULT = 5


def get_runtime_settings() -> dict:
    """Return the live Runtime-tab values from settings.json with safe defaults.

    Always returns exactly two keys so callers never hit a missing key:
      {"python_exec_auto_approve": "manual"|"auto",
       "python_exec_auto_approve_seconds": int}
    Bad/corrupt stored values fall back to the defaults (fail-closed = manual).
    """
    settings = _load_settings()

    mode = str(settings.get("python_exec_auto_approve") or "").strip().lower()
    if mode not in ("manual", "auto"):
        mode = PYTHON_EXEC_AUTO_APPROVE_DEFAULT

    try:
        seconds = int(str(settings.get("python_exec_auto_approve_seconds") or "").strip())
    except (TypeError, ValueError):
        seconds = 0
    if not (1 <= seconds <= 3600):
        seconds = PYTHON_EXEC_AUTO_APPROVE_SECONDS_DEFAULT

    return {
        "python_exec_auto_approve": mode,
        "python_exec_auto_approve_seconds": seconds,
    }

# Kept for backward compatibility: the module-level constant mirrors settings.json at
# import time ONLY. Live code paths must use get_connection_mode() / get_web_relay_address().
USE_WEB_SERVER: Optional[str] = (get_web_relay_address() if get_connection_mode() == "web_relay" else None)
# --- Direct WebSocket Mode (PC-to-PC encrypted, no relay server needed) ---
# SYNC RULE: the WEB_RELAY_* / WS_HANDSHAKE_TIMEOUT values below MUST stay identical to the
# "Web Relay / Direct WS Connection Constants" section in <repo>/config/config.py (SERVER truth)
# and web_server_relay/index.php (single-file pure-PHP relay). All three ends of every
# WebSocket link must agree on heartbeat, message size and handshake timeouts.
# PROTOCOL VERSION (2026-08-20): MUST match the SERVER's PROTOCOL_VERSION
# (<repo>/config/config.py). Sent in every auth frame, echoed back in auth_ok.
# A mismatch is rejected with an actionable error instead of silent misbehavior.
# (2026-09-08) v3: additive optional frame types queue_status / queue_start from the SERVER's
# multi-chat request queue. Unknown frame types are ignored by this client build.
# (2026-09-23) v4: relays now forward each client's API key + email in 'client_connected' so the
# BRAIN authenticates every relayed client against its OWN config/.api_keys.json (same logic as
# direct mode - the client never sends a role). auth_ok frames echo protocol_version on all paths.
PROTOCOL_VERSION: int = 4

COOLEMS_DIRECT_WS_PORT: int = 8080
WEB_RELAY_MAX_MESSAGE_SIZE: int = 1048576 * 10
WEB_RELAY_HEARTBEAT_INTERVAL: int = 30
WEB_RELAY_HEARTBEAT_TIMEOUT: int = 10   # Timeout (s) before a dead peer is dropped (ping_timeout on connect)
WS_HANDSHAKE_TIMEOUT: int = 10          # Timeout (s) for the WS auth handshake
WEB_RELAY_DEFAULT_PORT: int = 8443      # Default relay port when USE_WEB_SERVER has no explicit port

# --- Model Switch Timing (CLIENT side, single source of truth) ---
# The SERVER does its own reload wait internally (config.LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC,
# 240 s default - sized for a COLD load of a ~23 GB model; see the 2026-09-09 switch-timeout fix),
# so every CLIENT-side budget below must EXCEED that. These are bounded waits with clean
# status messages - there is NO fallback logic: we either confirm the new model loaded or
# report failure after MODEL_SWITCH_MAX_WAIT_SEC.
MODEL_SWITCH_PHASE1_TIMEOUT: int = 30   # (s) Phase 1 window: WS connect + auth + send command + await fast response
MODEL_SWITCH_POLL_INTERVAL: float = 2.0 # (s) between health polls while waiting for the server reload
MODEL_SWITCH_PROGRESS_LOG_SEC: int = 20 # (s) between "Waiting..." progress logs / UI broadcasts
MODEL_SWITCH_MAX_WAIT_SEC: int = 300    # (s) total Phase 2 wait budget before declaring failure (5 min)
MODEL_SWITCH_CLIENT_BUDGET_SEC: int = 330  # (s) executor timeout for the whole switch call; must exceed MAX_WAIT + margin

# --- Bootstrap wait (CLIENT startup -> SERVER delivery) ---
# The CLIENT often starts while the COOLEMS SERVER is still booting / loading its model,
# so bootstrap_framework() does NOT crash on a refused connection: it waits patiently for
# the SERVER to come up and deliver tools/DNA. Bounded by BOOTSTRAP_MAX_WAIT_SEC (env
# override: COOLEMS_CLIENT_BOOTSTRAP_MAX_WAIT). No fallback logic - after the budget is
# exhausted startup fails with ONE clean, actionable message.
BOOTSTRAP_MAX_WAIT_SEC: int = 1800          # total wait budget for SERVER delivery at startup (30 min)
BOOTSTRAP_RETRY_INITIAL_DELAY: float = 2.0  # first retry delay after a failed attempt (s)
BOOTSTRAP_RETRY_MAX_DELAY: float = 15.0     # backoff cap between retries (s)
BOOTSTRAP_PROGRESS_LOG_SEC: int = 30        # seconds between "waiting for SERVER" progress logs
BOOTSTRAP_ATTEMPT_TIMEOUT: float = 60.0     # per-attempt ceiling so a hung handshake never blocks the wait loop

# ============================================================
# TLS / SSL Certificate Verification Settings
# ============================================================
# When True, CLIENT verifies the SERVER's TLS certificate before trusting it.
# Requires copying SERVER's cert to CLIENT/certs/server_ca.crt (see SETUP_TLS.md).
# Default False for backward compatibility with self-signed certs on LAN.
# (2026-09-23) The relay runs on a PUBLIC host, so this link is internet-facing: when the
# CLIENT connects to it in web_relay mode the transport layer FAILS CLOSED unless either full
# CA verification (True - Let's Encrypt etc.) or a usable cert pin below is configured. There is
# no silent CERT_NONE fallback for relay connections anymore.
WEB_RELAY_VERIFY_SSL_CERTS: bool = False

# (2026-09-01 S3) Certificate PINNING for the client->relay link. Same contract as the
# SERVER-side WEB_RELAY_CERT_PIN_FINGERPRINT: leave empty for legacy behavior; set a
# "sha256/<base64-of-DER>" pin (or path to the relay .pem/.crt) and every other certificate
# is rejected at handshake time, before any API key crosses the wire.
WEB_RELAY_CERT_PIN_FINGERPRINT: str = ""

COOLEMS_DIRECT_VERIFY_SSL_CERTS: bool = False

# Path to the SERVER's CA certificate file for TLS verification.
# To enable verification:
#   1. Copy SERVER/certs/server.crt -> CLIENT/certs/server_ca.crt
#   2. Set COOLEMS_DIRECT_VERIFY_SSL_CERTS = True (and/or WEB_RELAY_VERIFY_SSL_CERTS = True)
COOLEMS_SERVER_CERT_PATH: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "certs", "server_ca.crt"
)

COOLEMS_RECONNECT_MIN_DELAY: float = 1.0
COOLEMS_RECONNECT_MAX_DELAY: float = 30.0

# --- Multi-Chat Background Sessions (2026-09-08) ---
# A chat's AI session keeps running in the CLIENT backend even while you view another chat.
CHAT_BACKGROUND_FRAME_BUFFER: int = 400   # frames kept per background chat until it finishes (bounded memory)
CLIENT_MAX_OPEN_CHAT_WS: int = 6          # UI LRU cap on simultaneously open per-chat sockets

# --- Model (fetched from SERVER at runtime) ---
MODEL_NAME: str = ""  # Auto-detected from SERVER loaded models at runtime do not hardcode here

# NOTE: CHROME_CDP_PORT is NOT defined here — the SERVER ships it to CLIENT-side browser tools via config_constants (server SoT).

# --- Provider Defaults ---
PROVIDER_DEFAULT_TIMEOUT: int = 9300    # HTTP/WS timeout (s) for provider API calls — single source of truth (was also exported as API_TIMEOUT; alias removed)
# Generation temperature default for CLIENT-side provider calls (mirrors the SERVER's LLAMA_SERVER_TEMP).
PROVIDER_DEFAULT_TEMPERATURE: float = 0.6

# --- Agentic Loop ---
AGENTIC_MAX_TOOL_ITERATIONS: int = 2000             # Max total tool iterations (prevents infinite loops)
AGENTIC_MAX_THINKING_ITERATIONS: int = 1            # Max consecutive thinking-only responses before forcing completion
AGENTIC_TOKEN_WARNING_THRESHOLD: float = 0.95     # Fraction of the safe token budget (max_history_tokens) where the console warning + context-guardian message fire
AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS: int = 3  # Max times per run the near-limit guardian may be injected - bounded so a pathological prompt cannot loop forever
AGENTIC_ITERATION_TIMEOUT_SEC: int = 180          # Timeout per single provider call iteration (3 minutes)
AGENTIC_TOOL_OUTPUT_MAX_CHARS: int = 160

# --- Database ---
# FIX (2026-08-17): anchor the DB file to the CLIENT start point (<CLIENT>/) instead of
# CWD-relative "chat_history.db" - a process started from another folder would otherwise
# create stray chat_history.db files outside the CLIENT tree. (This module lives at <CLIENT>/config/,
# so one level up is the CLIENT root where code_client.py sets its CWD.)
_DB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH: str = os.path.join(_DB_DIR, "chat_history.db")

# NOTE: web-search values (WEB_SEARCH_*) are NOT defined here. The SERVER is the single
# source of truth and ships them to CLIENT-side tools via tools_response 'config_constants'
# (see app/providers/coolems/tool_scanner.extract_config_constants on the server).

# --- File Handling ---
FILE_MAX_SIZE_BYTES: int = 2147483648
FILE_MAX_TEXT_CONTENT_BYTES: int = 5242880
FILE_MAX_WRITE_BYTES: int = 3221225472
FILE_EXEC_OUTPUT_TRUNCATE_CHARS: int = 10000
FILE_EXEC_ERROR_TRUNCATE_CHARS: int = 2000
FILE_EXEC_TIMEOUT: int = 300

# --- Image Generation / Self-Unpacking Tools (2026-08-23) ---
# Single source of truth is SERVER config/config.py; these local values keep CLIENT disk code
# (tool_executor, bootstrap) importable before any tools_response arrives. The SERVER ships the
# same names in tools_response 'config_constants' for delivered tool sources.
IMAGE_SUBPROCESS_TIMEOUT: int = 300                     # Max wait (s) for image gen subprocess
IMAGE_SUBPROCESS_TIMEOUT_OFFLOAD: int = 900             # Max wait (s) for GPU-offloaded tiers (FP8 + cpu offload on <16 GB cards): slower warm-up + first-run ~14 GB download safety net
TOOLS_BOOTSTRAP_TIMEOUT_SEC: int = 2400                 # Max wait (s) for a self-unpacking tool's first-run setup (venv + pip install)

# --- Derived Helpers ---


def get_api_url() -> str:
    """Return the API URL for the configured provider.

    For 'coolems_client' this is always empty - the client talks to the SERVER
    over WebSocket, not a local HTTP API. Any other provider raises ValueError
    because the CLIENT distribution supports only coolems_client.
    """
    if PROVIDER == "coolems_client":
        return ""  # Client connects via WebSocket, no local API URL needed
    raise ValueError(f"CLIENT cannot use provider '{PROVIDER}'. Only 'coolems_client' is supported.")


def get_provider_name() -> str:
    """Return the configured provider name (always 'coolems_client' in CLIENT)."""
    return PROVIDER
    

def max_history_tokens() -> int:
    """Get max history tokens from SERVER-provided context window.

    Raises RuntimeError if CONTEXT_WINDOW_TOKENS has not been set yet
    (i.e., CLIENT has not received auth_ok from SERVER).
    No fallback to hardcoded values allowed.
    """
    if CONTEXT_WINDOW_TOKENS is None:
        raise RuntimeError(
            "CONTEXT_WINDOW_TOKENS not set - SERVER handshake not completed. "
            "This should never happen in normal operation as context_window "
            "is received from SERVER during auth_ok."
        )
    return int(CONTEXT_WINDOW_TOKENS * HISTORY_CONTEXT_FRACTION)


def estimate_tokens(text: str) -> int:
    """Rough token estimate for a string using CHARS_PER_TOKEN.

    Used only for budgeting/trimming heuristics - the authoritative context
    window always comes from the SERVER (CONTEXT_WINDOW_TOKENS).
    Never returns 0 (empty text counts as 1 token).
    """
    if not text:
        return 1
    return max(1, round(len(text) / CHARS_PER_TOKEN))


# (2026-08-20 review #7/#8) chars_to_tokens() was deleted: it called len() on an int
# (always TypeError) and had zero callers. Live readers below replace import-time
# captures of CONTEXT_WINDOW_TOKENS / MODEL_NAME, which the SERVER rewrites at runtime
# via apply_auth_info() -- a name imported once at module load would keep the stale value.
def get_context_window() -> int:
    """Live read of CONTEXT_WINDOW_TOKENS (SERVER may update it after every auth)."""
    return CONTEXT_WINDOW_TOKENS


def get_model_name() -> str:
    """Live read of MODEL_NAME (boot-time default until SERVER reports the loaded model)."""
    return MODEL_NAME
