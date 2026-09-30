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


def _target_display() -> str:
    """Human-readable target for log messages (shows the full failover order)."""
    from config import get_connection_mode, get_web_relay_address, get_server_address_list
    if get_connection_mode() == "web_relay":
        return f"relay {get_web_relay_address() or '<not saved>'}"
    addrs = get_server_address_list() or ["<unset>"]
    return "direct " + ", ".join(addrs)


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
                logger.error(f"[BOOTSTRAP] Auth failed: {auth_resp!r}")
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
        return None
    except Exception as e:
        level, reason = _classify_failure(e)
        logger.log(getattr(logging, level.upper()), f"[BOOTSTRAP] Attempt failed - {reason}")
        return None


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

    deadline = time.monotonic() + budget
    delay = BOOTSTRAP_RETRY_INITIAL_DELAY
    attempt = 0
    next_progress_log = 0.0

    while True:
        attempt += 1
        try:
            response = await _bootstrap_fetch_once()
        except ConnectionError as e:
            logger.error(f"[BOOTSTRAP] FATAL: {e}")
            raise
        if response is not None:
            waited = time.monotonic() - (deadline - budget)
            logger.info(
                f"[BOOTSTRAP] SERVER delivered tools/DNA on attempt {attempt}"
                + (f" after waiting {waited:.0f}s" if waited >= 2 else "")
            )
            return response

        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            break

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

    logger.error(
        f"[BOOTSTRAP] Gave up after {attempt} attempts / {int(budget)}s waiting for the SERVER at {target}. "
        f"Start the COOLEMS SERVER first (code.py in the server root folder), make sure its address/port match "
        f"CLIENT config settings.json, then restart the CLIENT. Increase BOOTSTRAP_MAX_WAIT_SEC in "
        f"CLIENT/config/config.py if your model takes longer to load."
    )
    return None
