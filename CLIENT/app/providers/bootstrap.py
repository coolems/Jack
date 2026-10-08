"""Bootstrap framework fetch - runs BEFORE CoolemsClientProvider is instantiated.

Solves the chicken-and-egg problem introduced by the 2026-08-20 dedup change:
  - BaseProvider / TokenStats now live ONLY in SERVER (delivered via tools_response)
  - CoolemsClientProvider subclasses BaseProvider at class-definition time
  - But we cannot instantiate the provider until app.providers.base is in sys.modules

This module opens a RAW WebSocket connection (config values only - no provider
class needed), authenticates, sends ONE tools_request, and returns the full
tools_response. The caller then installs it via RemoteToolOrchestrator.install_from_response()
(same single install code path as every other init - no duplicated bookkeeping).

After that runs, `from app.providers import BaseProvider` resolves to the real
SERVER-delivered class and get_provider() can build CoolemsClientProvider.

Why a separate raw fetch instead of reusing tool_requests.send_tool_request()?
That helper takes a provider instance as its first argument - which is exactly the
object we cannot construct yet. Everything else (URL/SSL/auth helpers, protocol
check) IS reused from app.providers.coolems so there is one source of truth for
the wire format.

WAITING FOR SERVER (2026-08-26): the CLIENT commonly starts while the COOLEMS
SERVER is still booting / loading its model - a refused TCP connection at that
moment is NORMAL, not an error. bootstrap_framework() therefore waits patiently:
time-budgeted retries with capped backoff and periodic progress logs (see
BOOTSTRAP_* constants in config.py). It only gives up after BOOTSTRAP_MAX_WAIT_SEC
(env override COOLEMS_CLIENT_BOOTSTRAP_MAX_WAIT) - then the caller raises ONE clean,
actionable error. No fallback logic: SERVER delivery is still mandatory.
"""

import asyncio
import json
import logging
import os
import time

logger = logging.getLogger("COOLEMS.Provider.Bootstrap")

# (2026-10-05 KEY-DRIFT DX) One-shot diagnostics for the auth-rejection loop: how many
# consecutive attempts were rejected by SERVER with an *auth* error, and whether the
# actionable "key mismatch" hint has already been printed. Reset on every successful
# delivery so a later re-auth failure starts counting fresh.
_auth_reject_count = 0
_key_mismatch_hint_printed = False
_lan_hint_printed = False   # (2026-10-07) one-time hint when only local addresses are configured

# ---------------------------------------------------------------------------
# LIVE BOOT STATUS (2026-10-07 SETUP-MODE UI FIX)
# The CLIENT may now start its UI BEFORE bootstrap succeeds (setup mode: no key yet).
# GET /api/setup/boot reads this dict so the Settings modal can show what boot is
# doing RIGHT NOW instead of a dead page. Updated by _bootstrap_fetch_once() and
# bootstrap_framework(); consumed read-only - never mutated from outside this module.
# ---------------------------------------------------------------------------
_status = {
    "phase": "idle",            # idle | waiting_key | bootstrapping | ready | failed
    "attempts": 0,              # framework-fetch attempts made so far
    "last_error": "",           # short human-readable reason of the last attempt
    "budget_sec": 0.0,          # total wait budget for this run
    "remaining_sec": 0.0,       # seconds left in the budget (0 when done/failed)
}



def _target_display() -> str:
    """Human-readable target for log messages (shows the full failover order)."""
    from config import get_connection_mode, get_web_relay_address, get_server_address_list
    if get_connection_mode() == "web_relay":
        return f"relay {get_web_relay_address() or '<not saved>'}"
    addrs = get_server_address_list() or ["<unset>"]
    return "direct " + ", ".join(addrs)


def bootstrap_status() -> dict:
    """Live snapshot of the boot-time framework fetch (for GET /api/setup/boot).

    Read-only copy - callers must not mutate the module state. Phases:
      "waiting_key"   - no outbound API key configured yet; every SERVER auth attempt is
                        rejected with 'Empty API key'. Set one in UI Settings -> Authentication
                        (or CLIENT/config/.api_client_keys.json) and the NEXT attempt authenticates.
      "bootstrapping" - a key exists; waiting for the SERVER to deliver tools/DNA.
      "ready"         - framework delivered, boot can proceed to provider/agent init.
      "failed"        - wait budget exhausted without delivery (actionable message in log).
    """
    return dict(_status)

def _classify_failure(e: Exception) -> tuple[str, str]:
    """Map a failed bootstrap attempt to (log_level, short_reason).

    'server not up yet' is the EXPECTED case while the SERVER is still booting /
    loading its model - it must NOT spam scary ERROR lines. Anything else gets a
    real warning so genuine misconfigurations stay visible in the logs.
    """
    name = type(e).__name__
    if isinstance(e, ConnectionRefusedError):
        return "debug", "server not up yet (connection refused)"
    if isinstance(e, asyncio.TimeoutError):
        return "warning", f"timed out ({e})"
    if isinstance(e, OSError) and e.errno in (10061, 111):  # WinError 1225 / ECONNREFUSED
        return "debug", "server not up yet (connection refused)"
    if name == "InvalidURI":
        return "warning", f"invalid server address ({e})"
    if isinstance(e, OSError):
        return "warning", f"{name}: {e}"
    return "warning", f"{name}: {e}"


def _classify_auth_failure(auth_resp) -> str:
    """Map a non-auth_ok SERVER response to a short failure class (2026-10-05 KEY-DRIFT DX).

    'auth_rejected'  - SERVER refused the presented key ("Authentication failed or no
                       valid profile"): the CLIENT is presenting a key that does not match
                       config/.api_keys.json on the SERVER.
    'empty_key'      - (2026-10-07) SERVER rejected an EMPTY api_key: this machine has no
                       outbound credential configured yet (SETUP MODE). Waiting never helps
                       by itself - a key must be entered in UI Settings -> Authentication (which
                       writes CLIENT/config/.api_client_keys.json). The wait loop keeps retrying so the
                       very next attempt after the user sets the key succeeds.
    'protocol'       - explicit protocol version mismatch (fatal - retrying never helps).
    'other'          - anything else (malformed responses...).

    The class drives the wait-loop diagnostics: repeated 'auth_rejected' attempts print ONE
    actionable key-mismatch hint; a single 'empty_key' attempt prints the setup-mode hint.
    """
    if not isinstance(auth_resp, dict):
        return "other"
    msg = str(auth_resp.get("message", ""))
    if "Authentication failed or no valid profile" in msg:
        return "auth_rejected"
    if "Empty API key" in msg:  # (2026-10-07) setup mode - distinct from a wrong key
        return "empty_key"
    if "Protocol version mismatch" in msg:
        return "protocol"
    return "other"
async def _bootstrap_fetch_once() -> dict | None:
    """Open raw WS, auth (with protocol check), send tools_request, return response dict.

    Uses ONLY config values (address, port, key) - no provider class involved.
    Returns None on any failure; the retry wrapper decides whether to try again.

    Raises:
        ConnectionError: protocol version mismatch (fatal - retrying will never fix it).
    """
    import websockets
    from config import (
        COOLEMS_CLIENT_SERVER_ADDRESS,
        get_connection_mode,
        get_web_relay_address,
        WEB_RELAY_VERIFY_SSL_CERTS as _RELAY_VERIFY_SSL,
        WEB_RELAY_CERT_PIN_FINGERPRINT,
        WEB_RELAY_DEFAULT_PORT,
        COOLEMS_DIRECT_WS_PORT,
        WS_HANDSHAKE_TIMEOUT,
        WEB_RELAY_MAX_MESSAGE_SIZE,
        WEB_RELAY_HEARTBEAT_INTERVAL,
        WEB_RELAY_HEARTBEAT_TIMEOUT,
        WEB_RELAY_VERIFY_SSL_CERTS,
        COOLEMS_DIRECT_VERIFY_SSL_CERTS,
        COOLEMS_SERVER_CERT_PATH,
        PROTOCOL_VERSION,
    )
    from app.providers.coolems import (
        _build_websocket_url,
        _parse_web_relay_address,
        _check_protocol,
        apply_auth_info,
        connect_with_failover,
    )
    from app.providers.coolems.ssl_helper import (
        _build_verifying_ssl_context,
        _build_no_verify_ssl_context,
    )
    from app.keys import load_coolems_api_key

    # Determine the ordered URL + SSL candidate list (mirrors CoolemsClientProvider._get_ws_candidates).
    # Direct mode gets ONE candidate per configured address - local first, failover order.
    def _direct_ssl():
        return (
            _build_verifying_ssl_context(COOLEMS_SERVER_CERT_PATH)
            if COOLEMS_DIRECT_VERIFY_SSL_CERTS
            else _build_no_verify_ssl_context()
        )

    # (2026-09-23) Relay mode: the RELAY address from settings.json is the ONLY input -
    # no home-SERVER address involved. Fail-closed like every other relay path:
    # missing host or untrusted TLS raises (caught below -> retry loop reports it).
    def _relay_candidates():
        """Relay-mode candidates: ONE (url, ssl) pair from the live settings relay address."""
        addr = get_web_relay_address()
        if not addr:
            raise ConnectionError("Web relay mode selected but no relay host saved - open Settings -> Connection Mode and enter the web_server_relay address.")
        from app.providers.coolems.ssl_helper import _build_relay_ssl_context
        ssl_ctx = _build_relay_ssl_context(_RELAY_VERIFY_SSL, WEB_RELAY_CERT_PIN_FINGERPRINT)
        if ssl_ctx is None:
            raise ConnectionError("Web relay connection refused - no trusted TLS configured. Set WEB_RELAY_VERIFY_SSL_CERTS=True (public CA) or WEB_RELAY_CERT_PIN_FINGERPRINT.")
        host, port = _parse_web_relay_address(addr, WEB_RELAY_DEFAULT_PORT)
        return [(_build_websocket_url(host, port, True, "/ws/client"), ssl_ctx)]

    candidates = []
    if get_connection_mode() == "web_relay":
        try:
            candidates.extend(_relay_candidates())
        except Exception as e:
            logger.warning(f"[BOOTSTRAP] {e}")
            return None
    else:
        from config import get_server_address_list
        seen = set()
        for addr in get_server_address_list():
            # Same parsing logic as client_provider._parse_direct_ws_address (kept local on
            # purpose: importing client_provider here would execute `class CoolemsClientProvider(BaseProvider)`
            # with BaseProvider still undelivered -> the exact crash this module exists to prevent).
            if addr.startswith("["):
                bracket_end = addr.find("]")
                host = addr[1:bracket_end]
            elif ":" in addr:
                host = addr.rsplit(":", 1)[0]
            else:
                host = addr
            key = (host, COOLEMS_DIRECT_WS_PORT)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((_build_websocket_url(host, COOLEMS_DIRECT_WS_PORT, True), _direct_ssl()))
        if not candidates:  # settings empty/corrupt -> boot-time cached address keeps us alive
            addr = COOLEMS_CLIENT_SERVER_ADDRESS
            if addr.startswith("["):
                host = addr[1:addr.find("]")]
            elif ":" in addr:
                host = addr.rsplit(":", 1)[0]
            else:
                host = addr
            candidates.append((_build_websocket_url(host, COOLEMS_DIRECT_WS_PORT, True), _direct_ssl()))

    api_key = load_coolems_api_key()

    async def _do_fetch():
        async with connect_with_failover(
            candidates,
            max_size=WEB_RELAY_MAX_MESSAGE_SIZE,
            ping_interval=WEB_RELAY_HEARTBEAT_INTERVAL,
            ping_timeout=WEB_RELAY_HEARTBEAT_TIMEOUT,
        ) as ws:
            # Auth handshake (with protocol version) - same frame shape as every other path.
            await ws.send(json.dumps({
                "type": "auth",
                "api_key": api_key,
                "role_type": "client",
                "protocol_version": PROTOCOL_VERSION,
            }))
            auth_raw = await asyncio.wait_for(ws.recv(), timeout=WS_HANDSHAKE_TIMEOUT)
            auth_resp = json.loads(auth_raw)
            if not isinstance(auth_resp, dict) or auth_resp.get("type") != "auth_ok":
                # (2026-10-05 KEY-DRIFT DX + 2026-10-07 empty-key class): record the failure
                # class so the wait loop can tell "SERVER is up but rejects our key" apart
                # from "still booting" - and an EMPTY key (setup mode) gets its own hint.
                global _auth_reject_count, _key_mismatch_hint_printed, _status
                if _classify_auth_failure(auth_resp) == "auth_rejected":
                    _auth_reject_count += 1
                if _classify_auth_failure(auth_resp) == "empty_key":
                    logger.warning("[BOOTSTRAP] SERVER rejected the auth with an EMPTY key - no API key configured on this machine yet (SETUP MODE). Open the CLIENT UI: Settings -> Authentication -> Set API Key (writes CLIENT/config/.api_client_keys.json); the next attempt will authenticate.")
                else:
                    logger.error(f"[BOOTSTRAP] Auth failed: {auth_resp!r}")
                _status["last_error"] = "SERVER auth rejected (" + str(auth_resp.get("message", "")) + ")"
                return None
            _check_protocol(auth_resp, "bootstrap")
            # Apply auth-derived config (context_window -> CONTEXT_WINDOW_TOKENS) exactly
            # like every other auth path does - single mutation point in the coolems shell.
            apply_auth_info(auth_resp, context="bootstrap")

            # Request tools (full response includes framework_sources + shared_hashes).
            await ws.send(json.dumps({"type": "tools_request"}))
            resp_raw = await asyncio.wait_for(ws.recv(), timeout=30)
            resp = json.loads(resp_raw)
            if not isinstance(resp, dict) or resp.get("type") != "tools_response":
                logger.error(f"[BOOTSTRAP] Unexpected response type: {resp!r}")
                return None
            return resp

    try:
        # Per-attempt ceiling (config.BOOTSTRAP_ATTEMPT_TIMEOUT): a half-open TCP
        # connection or hung handshake must not stall the wait loop for minutes.
        from config import BOOTSTRAP_ATTEMPT_TIMEOUT
        return await asyncio.wait_for(_do_fetch(), timeout=BOOTSTRAP_ATTEMPT_TIMEOUT)
    except ConnectionError as e:
        # Protocol version mismatch is FATAL - retrying will never fix a version skew.
        # NOTE: on Windows ConnectionRefusedError/ConnectionResetError are SUBCLASSES of
        # ConnectionError, so only the explicit "Protocol version mismatch" message (raised
        # by _check_protocol) is fatal; every other connection error is logged and returns
        # None so the wait loop retries - that is exactly the SERVER-still-booting case.
        if "Protocol version mismatch" in str(e):
            raise
        level, reason = _classify_failure(e)
        logger.log(getattr(logging, level.upper()), f"[BOOTSTRAP] Attempt failed - {reason}")
        _status["last_error"] = "SERVER unreachable - " + reason
        return None
    except Exception as e:
        level, reason = _classify_failure(e)
        logger.log(getattr(logging, level.upper()), f"[BOOTSTRAP] Attempt failed - {reason}")
        _status["last_error"] = "bootstrap attempt failed - " + reason
        return None


def _refresh_phase(budget: float) -> None:
    """Keep _status["phase"] honest (2026-10-07 setup-mode UI).

    No key available right now  -> "waiting_key"  (the actionable state the user must fix);
    a key exists                 -> "bootstrapping" (normal SERVER-still-loading wait).
    """
    from app.keys import load_coolems_api_key as _lck2
    try:
        has_key = bool(_lck2())
    except Exception:  # pragma: no cover - keys module must never break the boot loop
        has_key = False
    _status["phase"] = "bootstrapping" if has_key else "waiting_key"
    _status["budget_sec"] = float(budget)

def _all_addresses_loopback() -> bool:
    """True when EVERY configured SERVER address is a loopback one (2026-10-07 LAN hint).

    A fresh machine ships with ['localhost:8080'] only; if the real SERVER lives on
    another LAN box, every attempt fails with NoServerReachable and this helper lets the
    wait loop say so explicitly instead of retrying localhost for 30 minutes.
    """
    try:
        from config import get_server_address_list
        addrs = [a for a in get_server_address_list() if a]
    except Exception:
        return False  # cannot tell - do not print a possibly-wrong hint
    if not addrs:
        return False
    import ipaddress as _ip

    def _host_of(addr: str) -> str:
        h = addr[1:addr.find("]")] if addr.startswith("[") else (addr.rsplit(":", 1)[0] if ":" in addr else addr)
        return h.strip().lower()

    for a in addrs:
        h = _host_of(a)
        if h in ("localhost", "::1"):
            continue
        try:
            if not _ip.ip_address(h).is_loopback:
                return False
        except ValueError:
            return False  # hostname we cannot resolve here - assume non-local, stay quiet
    return True

async def bootstrap_framework() -> dict | None:
    """Async entry point for the boot-time framework fetch (2026-09 threadless refactor).

    Fetches the full tools_response from SERVER and WAITS for it when the SERVER is
    not up yet (the normal case right after boot, while it loads its model):

      - time budget: config.BOOTSTRAP_MAX_WAIT_SEC (default 30 min; env override
        COOLEMS_CLIENT_BOOTSTRAP_MAX_WAIT in seconds)
      - backoff between attempts: BOOTSTRAP_RETRY_INITIAL_DELAY growing per attempt,
        capped at BOOTSTRAP_RETRY_MAX_DELAY
      - progress log every BOOTSTRAP_PROGRESS_LOG_SEC so the user sees the CLIENT is
        alive and waiting (not frozen/crashed)

    The old sync version ran a time.sleep retry loop that asyncio.run()ed one fetch
    per attempt. It is now a single async coroutine awaited from the boot sequence
    (code_client.py wraps init_provider_and_app in ONE top-level asyncio.run before
    uvicorn starts), so the whole wait happens on one event loop - no threads, no
    nested loops.

    Returns:
        The full tools_response dict for the caller to install via
        RemoteToolOrchestrator.install_from_response(), or None after the wait budget
        is exhausted.

    Raises:
        ConnectionError: protocol version mismatch (fatal - do not retry).
    """
    from config import (
        BOOTSTRAP_MAX_WAIT_SEC,
        BOOTSTRAP_RETRY_INITIAL_DELAY,
        BOOTSTRAP_RETRY_MAX_DELAY,
        BOOTSTRAP_PROGRESS_LOG_SEC,
    )

    try:
        budget = float(os.environ.get("COOLEMS_CLIENT_BOOTSTRAP_MAX_WAIT", "") or BOOTSTRAP_MAX_WAIT_SEC)
    except (TypeError, ValueError):
        budget = float(BOOTSTRAP_MAX_WAIT_SEC)
    if budget <= 0:
        budget = float(BOOTSTRAP_MAX_WAIT_SEC)

    target = _target_display()
    logger.info(f"[BOOTSTRAP] Waiting for COOLEMS SERVER at {target} to deliver tools/DNA (budget: {int(budget)}s)")

    # One-time startup diagnostic (2026-10-05 KEY-DRIFT DX): show WHICH outbound key will be
    # presented so "SERVER rejects auth" is diagnosable from the CLIENT log alone - no key
    # material ever printed, only a fingerprint + source. Setup mode says it plainly: nothing
    # can authenticate until a key exists (UI Settings -> API key, which writes the CLIENT config file).
    from app.keys import load_coolems_api_key as _lck
    _key_now = _lck()
    if not _key_now:
        logger.warning(
            "[BOOTSTRAP] No API key available for outbound auth - SETUP MODE. Every SERVER "
            "auth attempt will be rejected until a key is configured (UI Settings -> API key, "
            "writes CLIENT/config/.api_client_keys.json)."
        )
    else:
        import hashlib as _hashlib
        logger.info(
            f"[BOOTSTRAP] Outbound auth key ready (source=runtime/config-file/env; fingerprint "
            f"{_hashlib.sha256(_key_now.encode()).hexdigest()[:12]}). If SERVER keeps rejecting it, "
            "the presented key does not match config/.api_keys.json on the SERVER."
        )

    deadline = time.monotonic() + budget
    delay = BOOTSTRAP_RETRY_INITIAL_DELAY
    attempt = 0
    next_progress_log = 0.0

    # (2026-10-07 setup-mode UI): the phase starts as waiting_key when no key exists yet;
    # _refresh_phase() below re-evaluates it on every attempt, so a key set via the UI
    # while we wait flips the next attempt to bootstrapping automatically.
    _status["phase"] = "bootstrapping"

    while True:
        _refresh_phase(budget)
        attempt += 1
        _status["attempts"] = attempt
        try:
            response = await _bootstrap_fetch_once()
        except ConnectionError as e:
            logger.error(f"[BOOTSTRAP] FATAL: {e}")
            raise
        global _auth_reject_count, _key_mismatch_hint_printed
        if response is not None:
            # Successful delivery - clear any accumulated auth-rejection diagnostics.
            _auth_reject_count = 0
            _key_mismatch_hint_printed = False
            waited = time.monotonic() - (deadline - budget)
            logger.info(
                f"[BOOTSTRAP] SERVER delivered tools/DNA on attempt {attempt}"
                + (f" after waiting {waited:.0f}s" if waited >= 2 else "")
            )
            _status["phase"] = "ready"
            _status["remaining_sec"] = 0.0
            _status["last_error"] = ""
            return response

        now = time.monotonic()
        remaining = deadline - now
        _status["remaining_sec"] = max(remaining, 0.0)
        if remaining <= 0:
            break

        # (2026-10-05 KEY-DRIFT DX): SERVER is UP but rejects our key -> after 3 consecutive
        # auth rejections print ONE actionable hint (the retry loop itself stays - the user may
        # fix the key while we wait, and we must succeed on that attempt).
        if _auth_reject_count >= 3 and not _key_mismatch_hint_printed:
            _key_mismatch_hint_printed = True
            logger.warning(
                f"[BOOTSTRAP] SERVER is reachable but has rejected our API key {_auth_reject_count}x in a row - "
                "this will NOT fix itself by waiting. The CLIENT's outbound key (CLIENT/config/.api_client_keys.json / UI) "
                "does not match config/.api_keys.json on the SERVER. Fix: re-set your API key in UI Settings -> Authentication "
                "(it writes CLIENT/config/.api_client_keys.json), then restart this CLIENT."
            )

        # (2026-10-07 LAN-HINT DX): every configured address is loopback and none answered
        # -> the SERVER almost certainly runs on ANOTHER machine. One actionable line so a
        # fresh machine does not retry localhost for 30 minutes in silence.
        global _lan_hint_printed
        if (
            not _lan_hint_printed
            and str(_status.get("last_error", "")).startswith("SERVER unreachable")
            and _all_addresses_loopback()
        ):
            _lan_hint_printed = True
            logger.warning(
                "[BOOTSTRAP] No SERVER reachable at the configured address(es) - all of them are LOCAL (localhost/127.0.0.1). "
                "If your COOLEMS SERVER runs on another machine, add its LAN IP to server_addresses in CLIENT/config/settings.json "
                "(e.g. localhost:8080 plus 192.168.x.x:8080) or use UI Settings -> Server Address; the next attempt picks it up automatically."
            )

        # Progress log on a steady cadence - the user must see "still waiting", not silence.
        if now >= next_progress_log or attempt == 1:
            logger.info(
                f"[BOOTSTRAP] SERVER not ready yet (attempt {attempt}) - still waiting, "
                f"{remaining:.0f}s left in budget. This is normal while the SERVER loads its model."
            )
            next_progress_log = now + BOOTSTRAP_PROGRESS_LOG_SEC

        sleep_for = min(delay, max(remaining, 0))
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

        delay = min(delay * 1.5, BOOTSTRAP_RETRY_MAX_DELAY)

    _status["phase"] = "failed"
    if not _status.get("last_error"):
        _status["last_error"] = f"SERVER did not deliver tools/DNA within {int(budget)}s"
    _status["remaining_sec"] = 0.0
    logger.error(
        f"[BOOTSTRAP] Gave up after {attempt} attempts / {int(budget)}s waiting for the SERVER at {target}. "
        f"Start the COOLEMS SERVER first (code.py in the server root folder), make sure its address/port match "
        f"CLIENT config settings.json, then restart the CLIENT. Increase BOOTSTRAP_MAX_WAIT_SEC in "
        f"CLIENT/config/config.py if your model takes longer to load."
    )
    return None
