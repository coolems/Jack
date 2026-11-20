"""COOLEMS CLIENT configuration. All client-side values set here. NO server secrets.

CLIENT ONLY supports the 'coolems_client' provider: every request goes through the
SERVER via WebSocket relay, direct LLM access is BLOCKED in CLIENT code. Server address
comes from the Settings UI -> settings.json, the SINGLE SOURCE OF TRUTH. Server config
with secrets lives in root config/config.py, never in the CLIENT distribution."""

import logging
import os
from typing import Optional


# --- Context / Token Limits ---
CONTEXT_WINDOW_TOKENS: int | None = None  # Set dynamically from SERVER during auth handshake - NO fallback
HISTORY_CONTEXT_FRACTION: float = 0.90
CHARS_PER_TOKEN: float = 4.0
SYSTEM_PROMPT_TOKENS_ESTIMATE: int = 2000
IMAGE_TOKENS_PER_512PX: int = 4000

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


# --- Proxy Header Trust ---
# When True, X-Forwarded-For may override the socket address as the client identity
# (conversations.ip_address = workspace owner). The header is client-controlled, so it
# is honoured only behind a reverse proxy you trust.
TRUST_PROXY_HEADERS: bool = False


# ============================================================
# Settings Persistence - MUST be defined BEFORE anything uses it
# settings.json is the SINGLE SOURCE OF TRUTH for server_address
# ============================================================

_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def _load_settings() -> dict:
    """Load settings from settings.json. Returns {} on failure."""
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
# The Settings UI reads/writes it through /api/search-engines (app/routers/agent/);
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

    Missing/corrupt file: logs an error and returns [] - no fallback engines anywhere."""
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
    """Validate and write the engine list to search_engines.json (pretty-printed).

    Raises ValueError when validation fails; the file is left untouched."""
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
    """Auto-detect this machine's primary LAN IP ('192.168.x.x'), else 'localhost'.

    UDP socket trick: finds the outgoing interface IP without connecting."""
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
    """Fresh read of the ordered SERVER failover address list from settings.json.

    Index 0 is tried FIRST (put 'localhost:8080' there), the rest only when the previous is
    "server_addresses" (preferred) and "server_address"; the list wins and a singular value
    not already in it is appended.
    """
    settings = _load_settings()
    addrs = _normalize_server_addresses(settings.get("server_addresses"))
    for a in _normalize_server_addresses(settings.get("server_address")):
        if a not in addrs:
            addrs.append(a)
    return addrs


def get_good_server() -> str | None:
    """Return the last CONFIRMED-GOOD server address, or None.

    Persisted under 'server_address', which every reader already merges into the list."""
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
    """Persist *addr* as the known-good server, so a reboot goes straight to it.

    No-op on failure - in-process stickiness still works without the file write."""
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

    The known-good address when recorded and still listed, else the first entry of
    get_server_address_list. Empty/missing -> auto-detect the LAN IP, write it, return it.
    No env overrides, no fallbacks. Called once at import for boot caching and UI display.
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
    """Connection mode for the SERVER link ('relay' | 'direct').

    settings.json is the source of truth ('connection_mode'); env vars override it.
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
    """Host and port of the web relay.

    relay_host + relay_port in settings.json are the source of truth;
    COOLEMS_RELAY_HOST/PORT env vars override them.
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
#   image_gen_tier                      : "auto" | "high" | "mid" | "low"  (2026-10-03, default "auto")
#       Which model line generate_image() uses. 'auto' = classify by probed GPU VRAM;
#       the other three pin that tier (same semantics as SERVER config IMAGE_GEN_TIER_OVERRIDE).
#   image_gen_width / image_gen_height  : int pixels (2026-10-03, defaults 600 / 400)
#       The DEFAULT resolution generate_image() uses when the caller does not pass an
#       explicit size. Stored per-machine; read LIVE by the tool on every generation -
#       a save in the UI applies to the very next image (no restart). The worker still
#       snaps values down to multiples of 32 and clamps them to the tier's max_pixels
#       budget, so an oversized value degrades gracefully instead of breaking.
# All are read LIVE from settings.json on every access so a save in the UI applies to the
# very next dialog / image generation - no restart needed.

PYTHON_EXEC_AUTO_APPROVE_DEFAULT = "manual"
PYTHON_EXEC_AUTO_APPROVE_SECONDS_DEFAULT = 5
IMAGE_GEN_TIER_DEFAULT = "auto"
IMAGE_GEN_TIERS = ("auto", "high", "mid", "low")
#: (2026-10-03) Default image resolution for the Runtime tab - matches the tool's own
#: defaults so an untouched settings.json behaves exactly like before this setting existed.
IMAGE_GEN_WIDTH_DEFAULT = 600
IMAGE_GEN_HEIGHT_DEFAULT = 400
#: Inclusive pixel bounds accepted by POST /api/settings (the worker snaps to multiples of 32).
IMAGE_GEN_SIZE_MIN, IMAGE_GEN_SIZE_MAX = 32, 4096

# (2026-07-18 unified consent dialog) PER-KIND auto-approve timers for the OTHER three
# consent card kinds. Fully DETACHED from python_exec's pair and from each other: every
# kind has its own mode + seconds, so any dialog category can be set to auto-allow
# independently in Settings > Runtime. Defaults are "manual" (never auto) - safe until
# the user explicitly opts a category into auto-approve. Keys follow the same pattern as
# python_exec_auto_approve[_seconds] but with per-kind names:
#   url_consent_auto_approve / url_consent_auto_approve_seconds
#   file_consent_auto_approve / file_consent_auto_approve_seconds
#   network_consent_auto_approve / network_consent_auto_approve_seconds
#: Consent kinds with a card. 'desktop' (2026-11-07) gates Computer Use: binding an app
#: handle, listing running apps, capturing a window, or sending input. It is the only
#: kind whose payload is an APP identity rather than code/URL/path/socket, because the
#: thing being approved is "may this AI drive that application?".
#: 'question' is the ask_user_question card: a QUESTION + options, answered with
#: free text. It is listed here so every kind-driven loop stays complete, but it has NO
#: UI row in Settings > Runtime and its pair is pinned to 'manual' - auto-answering a
#: question would invent an answer the user never gave.
CONSENT_KINDS = ("python", "url", "file", "network", "subprocess", "desktop", "question")
#: settings.json key names per kind (python keeps its historical python_exec_* keys).
CONSENT_SETTING_KEYS = {
    "python":  ("python_exec_auto_approve",        "python_exec_auto_approve_seconds"),
    "url":     ("url_consent_auto_approve",        "url_consent_auto_approve_seconds"),
    "file":    ("file_consent_auto_approve",       "file_consent_auto_approve_seconds"),
    "network": ("network_consent_auto_approve",    "network_consent_auto_approve_seconds"),
    "subprocess": ("subprocess_consent_auto_approve", "subprocess_consent_auto_approve_seconds"),  # (2026-10-24)
    "desktop": ("desktop_consent_auto_approve",    "desktop_consent_auto_approve_seconds"),  # (2026-11-07)
    "question": ("question_consent_auto_approve",  "question_consent_auto_approve_seconds"),  # pinned manual
}
#: Kinds that can NEVER be auto-answered, whatever settings.json says (fail-closed).
CONSENT_NEVER_AUTO_KINDS = frozenset({"question"})
CONSENT_AUTO_APPROVE_DEFAULT = "manual"
CONSENT_AUTO_APPROVE_SECONDS_DEFAULT = 5

#: The ask_user_question card is tinted with its OWN colour, so a question
#: reads apart from the amber consent cards. Settings > Runtime stores it in settings.json
#: as 'question_dialog_color' (#rrggbb); the UI applies it live as the CSS variable
#: --question-accent, so a pick recolours every card on screen without a restart.
QUESTION_DIALOG_COLOR_DEFAULT = "#22c55e"
#: The one accepted shape: '#' + exactly six hex digits.
QUESTION_DIALOG_HEX_LEN = 6
_HEX_DIGITS = "0123456789abcdefABCDEF"


def is_question_dialog_color(raw) -> bool:
    """True when *raw* is exactly '#rrggbb'. POST /api/settings rejects anything else."""
    s = str(raw or "").strip()
    return (len(s) == 1 + QUESTION_DIALOG_HEX_LEN and s[0] == "#"
            and all(c in _HEX_DIGITS for c in s[1:]))


def _coerce_question_dialog_color(value) -> str:
    """A stored question-card colour lowercased, or the default when it is not #rrggbb.

    Hand-checked, so config.py stays import-light and a corrupt settings.json never paints
    an unreadable colour (fail-closed)."""
    return str(value).strip().lower() if is_question_dialog_color(value) else QUESTION_DIALOG_COLOR_DEFAULT


def get_runtime_settings() -> dict:
    """Return the live Runtime-tab values from settings.json with safe defaults.

    Always exactly six keys: python_exec_auto_approve, python_exec_auto_approve_seconds,
    image_gen_tier, image_gen_width, image_gen_height, question_dialog_color. Bad stored
    values fall back to the defaults (fail-closed = manual/auto)."""
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

    tier = str(settings.get("image_gen_tier") or "").strip().lower()
    if tier not in IMAGE_GEN_TIERS:
        tier = IMAGE_GEN_TIER_DEFAULT

    # (2026-10-03) Default resolution - out-of-range / corrupt values fall back to the
    # tool's own defaults so a bad settings.json can never break generation.
    width = _coerce_image_gen_size(settings.get("image_gen_width"), IMAGE_GEN_WIDTH_DEFAULT)
    height = _coerce_image_gen_size(settings.get("image_gen_height"), IMAGE_GEN_HEIGHT_DEFAULT)

    # Question-card tint. A bad/absent value falls back to the default green,
    # so a corrupt settings.json can never paint the card with an unreadable colour.
    question_color = _coerce_question_dialog_color(settings.get("question_dialog_color"))

    # (2026-07-18 unified consent dialog) per-kind auto-approve settings, fully detached:
    # approvals[kind] = {"mode": "manual"|"auto", "seconds": int 1..3600} for all four
    # kinds. The frontend starts a countdown from THAT kind's values when rendering the
    # card; manual = wait forever (the original behavior).
    approvals = {}
    for _kind in CONSENT_KINDS:
        _mode_key, _sec_key = CONSENT_SETTING_KEYS[_kind]
        if _kind == "python":  # historical defaults keep their values
            _def_mode, _def_sec = PYTHON_EXEC_AUTO_APPROVE_DEFAULT, PYTHON_EXEC_AUTO_APPROVE_SECONDS_DEFAULT
        else:
            _def_mode, _def_sec = CONSENT_AUTO_APPROVE_DEFAULT, CONSENT_AUTO_APPROVE_SECONDS_DEFAULT
        _mode = str(settings.get(_mode_key) or "").strip().lower()
        if _mode not in ("manual", "auto"):
            _mode = _def_mode
        # 'question' is pinned manual no matter what settings.json says: an
        # auto-answered question would invent an answer the user never gave.
        if _kind in CONSENT_NEVER_AUTO_KINDS:
            _mode = "manual"
        try:
            _sec = int(str(settings.get(_sec_key) or "").strip())
        except (TypeError, ValueError):
            _sec = 0
        if not (1 <= _sec <= 3600):
            _sec = _def_sec
        approvals[_kind] = {"mode": _mode, "seconds": _sec}
    return {
        "python_exec_auto_approve": mode,
        "python_exec_auto_approve_seconds": seconds,
        "image_gen_tier": tier,
        "image_gen_width": width,
        "image_gen_height": height,
        # Question-card tint, read live so the UI recolours on the next card.
        "question_dialog_color": question_color,
        # (2026-07-18) per-kind consent auto-approve settings (see CONSENT_SETTING_KEYS).
        "approvals": approvals,
    }


def _coerce_image_gen_size(value, default: int) -> int:
    """Coerce a stored image-gen dimension to an int inside IMAGE_GEN_SIZE_MIN..MAX.

    A non-int or out-of-range value degrades to the default instead of reaching generation."""
    try:
        v = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    if not (IMAGE_GEN_SIZE_MIN <= v <= IMAGE_GEN_SIZE_MAX):
        return default
    return v

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
PROTOCOL_VERSION: int = 5

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

# --- Backend-outage resilience (2026-11-19 Strata OOM / engine restart) ---
# When the SERVER tags a chat error as 'backend_unavailable' (HTTP 503, OOM reserve, no-progress
# watchdog, expert reload), the CLIENT preserves the in-flight call and resumes it instead of
# failing the turn. These bound that wait and the switch-model escape hatch.
BACKEND_RESUME_POLL_SEC: float = 5.0       # (s) between health polls while the backend reloads
BACKEND_RESUME_MAX_WAIT_SEC: float = 600.0 # (s) total wait budget before giving up (covers Strata's ~92 GB expert reload)
BACKEND_OFFER_SWITCH_AFTER_SEC: float = 45.0  # (s) down before the switch-model question card is offered
BACKEND_STATUS_POLL_SEC: float = 5.0       # (s) self-poll cadence for the dialog's live status (fallback when no fleet_status push)

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

# --- Live steering (2026-10-23) ---
# While an agentic task is running, the user can type in the prompt box and press Enter:
# the text is queued on the chat's channel and injected into the ReAct loop at its next
# safe step (never as a new turn, never stopping the current task). Bounded so a stuck
# keyboard cannot grow memory without limit; oldest entries are dropped first.
CHAT_STEER_QUEUE_MAX: int = 32

# --- ask_user_question ---
# The card the AI asks the human with. 'blocking' waits with no time limit (the session
# clock pauses); 'timed' waits at most QUESTION_TIMEOUT_DEFAULT_SEC, closes the card and
# returns TIMEOUT - not a refusal, the AI decides and states its assumption.
QUESTION_TIMEOUT_DEFAULT_SEC: int = 120
#: Candidates the card may show. More than this cannot be read in one glance.
QUESTION_MAX_OPTIONS: int = 6

# --- MCP servers ---
# MCP servers are declared in CLIENT/client_config/mcp_servers.json and are DISABLED by default:
# an MCP server is an external process or remote endpoint, so enabling it is a human edit,
# and its first tool call still asks a consent card ('subprocess' for stdio, 'url' for http).
# An http endpoint additionally passes the SSRF gate (app/mcp/net_guard.py). These constants
# bound what reaches the context window, not whether a server is allowed.
MCP_TEXT_PREVIEW_CHARS: int = 4000      # payload preview handed to the model (rest spills)
MCP_INSTRUCTIONS_MAX_CHARS: int = 1200  # server instructions as untrusted data in the prompt
MCP_TIMEOUT_DEFAULT_SEC: int = 30       # per-request wall clock when an entry sets none
MCP_TIMEOUT_MAX_SEC: int = 300          # clamp for an entry's timeout_sec
MCP_SPILL_KEEP_FILES: int = 20          # .temp/mcp/* payloads kept for full results

# --- Model (fetched from SERVER at runtime) ---
MODEL_NAME: str = ""  # Auto-detected from SERVER loaded models at runtime do not hardcode here

# NOTE: CHROME_CDP_PORT is NOT defined here — the SERVER ships it to CLIENT-side browser tools via config_constants (server SoT).

# --- Provider Defaults ---
PROVIDER_DEFAULT_TIMEOUT: int = 9300    # HTTP/WS timeout (s) for provider API calls — single source of truth (was also exported as API_TIMEOUT; alias removed)
# Generation temperature default for CLIENT-side provider calls (mirrors the SERVER's LLAMA_SERVER_TEMP).
PROVIDER_DEFAULT_TEMPERATURE: float = 0.6

# --- Agentic Loop ---
AGENTIC_MAX_TOOL_ITERATIONS: int = 2000             # Max total tool iterations (prevents infinite loops)
#: Max consecutive marker-free prose replies before the loop asks for a final answer.
#: (2026-11-07 fix) Was 1, which ended productive runs mid-task: ONE prose turn between two
#: tool batches (a summary, a plan write, an answer to a mid-run question) immediately
#: injected "provide your final answer and end this agentic loop", so a run stopped at ~48%
#: of the context window with ~133K tokens still free. The user read that as a context-guardian
#: stop because the notice named no cause and earlier guardian notices sat in history.
#: 3 = a genuine stall (three marker-free replies with no tool work) still ends the run.
AGENTIC_MAX_THINKING_ITERATIONS: int = 2
AGENTIC_TOKEN_WARNING_THRESHOLD: float = 0.95     # Fraction of the MESSAGE-LIST budget (max_history_tokens) where the console/UI "trimming is about to engage" warning fires
#: (2026-11-07) Fraction of the FULL context window (CONTEXT_WINDOW_TOKENS, the number the UI
#: status bar shows as "N K ctx") at which the context-guardian [SYSTEM NOTICE] is injected.
#: It used to be measured against max_history_tokens() (= HISTORY_CONTEXT_FRACTION 0.90 of the
#: window) AND quoted as a percentage OF that budget, so the notice said "96% of the safe
#: budget" while the status bar still showed ~100K tokens free - a contradiction the user
#: reported. The threshold is now a fraction of the window itself and the notice quotes sent /
#: left against that same window, so guardian and UI can never disagree. Default 0.90 = the
#: moment the history budget is reached, which is exactly when trimming starts.
AGENTIC_CONTEXT_GUARDIAN_WINDOW_FRACTION: float = 0.90
AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS: int = 3  # Max times per run the near-limit guardian may be injected - bounded so a pathological prompt cannot loop forever
#: (2026-11-09) The old AGENTIC_CONTEXT_GUT_FRACTION ("a trim pass that removed >= 50% of the
#: context is a GUT") was REMOVED: it gated nothing. With the LOOP RULE (utils/history_trimmer
#: safe_trim_history loop_start) a big pass can only eat PREVIOUS turns of the session, which is
#: legitimate trimming - never a reason to stop the run. The guardian fires on TRIM NO-OP
#: (stuck_at_limit) instead; the trim log still shows "before -> after" + removed=N tok, so a
#: large discard stays visible without a second threshold to maintain.
AGENTIC_ITERATION_TIMEOUT_SEC: int = 180          # Timeout per single provider call iteration (3 minutes)
# UI frame budget for the `tool_end` notification only (the chat's last-tool strip shows the
# real result). The ReAct history is NOT trimmed here - it keeps what the tool returned
# (image data sanitized separately). 160 chars hid the whole tool result from the UI.
AGENTIC_TOOL_OUTPUT_MAX_CHARS: int = 6000

# --- Database ---
# FIX (2026-08-17): anchor the DB file to the CLIENT start point (<CLIENT>/) instead of
# CWD-relative "chat_history.db" - a process started from another folder would otherwise
# create stray chat_history.db files outside the CLIENT tree. (This module lives at <CLIENT>/client_config/,
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
IMAGE_SUBPROCESS_TIMEOUT_OFFLOAD: int = 1800            # Max wait (s) for GPU-offloaded tiers (v21: the Qwen high tier streams a 20B MMDiT through CPU offload at every step; low tier likewise): slower warm-up + first-run multi-GB download safety net
TOOLS_BOOTSTRAP_TIMEOUT_SEC: int = 10800                # Max wait (s) for a self-unpacking tool's first-run setup (venv + pip install + large model download; 3h safety net so slow links can finish multi-GB downloads)

# --- Derived Helpers ---


def get_api_url() -> str:
    """Return the API URL for the configured provider.

    Always empty for 'coolems_client' (the CLIENT talks to the SERVER over WebSocket); any
    other provider raises ValueError."""
    if PROVIDER == "coolems_client":
        return ""  # Client connects via WebSocket, no local API URL needed
    raise ValueError(f"CLIENT cannot use provider '{PROVIDER}'. Only 'coolems_client' is supported.")


def get_provider_name() -> str:
    """Return the configured provider name (always 'coolems_client' in CLIENT)."""
    return PROVIDER
    

def max_history_tokens() -> int:
    """Max history tokens from the SERVER-provided context window.

    Raises RuntimeError when CONTEXT_WINDOW_TOKENS has not arrived yet - no hardcoded value."""
    if CONTEXT_WINDOW_TOKENS is None:
        raise RuntimeError(
            "CONTEXT_WINDOW_TOKENS not set - SERVER handshake not completed. "
            "This should never happen in normal operation as context_window "
            "is received from SERVER during auth_ok."
        )
    return int(CONTEXT_WINDOW_TOKENS * HISTORY_CONTEXT_FRACTION)


def estimate_tokens(text: str) -> int:
    """Rough token estimate using CHARS_PER_TOKEN - budgeting/trimming only.

    Never returns 0 (empty text counts as 1 token)."""
    if not text:
        return 1
    return max(1, round(len(text) / CHARS_PER_TOKEN))


# (2026-08-20 review #7/#8) chars_to_tokens() was deleted: it called len() on an int
# (always TypeError) and had zero callers. Live readers below replace import-time
# captures of CONTEXT_WINDOW_TOKENS / MODEL_NAME, which the SERVER rewrites at runtime
# via apply_auth_info() -- a name imported once at module load would keep the stale value.
def get_context_window() -> int:
    """Live read of CONTEXT_WINDOW_TOKENS (SERVER may it after every auth)."""
    return CONTEXT_WINDOW_TOKENS


def get_model_name() -> str:
    """Live read of MODEL_NAME (boot-time default until SERVER reports the loaded model)."""
    return MODEL_NAME
