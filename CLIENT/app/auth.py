"""
COOLEMS CLIENT - Authentication Module (CLIENT-SIDE ONLY)

CLIENT does NOT validate API keys or manage permissions locally.
All authorization happens on SERVER via WebSocket (configurable port, default 8080).

This module provides minimal HTTP middleware for local-mode detection only:
  - SETUP MODE (no real key configured yet): EVERYTHING is locked down except the
    loopback-only POST /api/auth/set-key endpoint that writes the first key. No chat,
    no files, no code execution before a key exists (2026-09-01 fix).
  - KEYED MODE: a real key must be available locally (CLIENT/config/.api_client_keys.json,
    COOLEMS_CLIENT_API_KEY env var, or the in-memory runtime key from this UI session)
    so we know which credential to present to the SERVER + email header mandatory.

REAL auth: CLIENT sends email + API key to SERVER -> SERVER validates against
its own config/.api_keys.json and applies restrictions from profiles.json. The server's
auth_ok / tools_response messages carry the authoritative role, allowed_models
and allowed_tools — the CLIENT never resolves permissions from disk locally.


SECURITY (2026-08-25) - hardened after code review:
  1. /files/ is NO LONGER a public prefix. File downloads are served through the
     authenticated /api/download endpoint instead, so unauthenticated LAN hosts can
     no longer read arbitrary files from working_root (e.g. config/.api_client_keys.json).
     Only / and /static/ remain public (UI shell + assets).
  2. In LOCAL MODE a request is only allowed when it comes FROM loopback AND the
     Host header also points at loopback. This closes the DNS-rebinding hole where an
     attacker page on another machine could fetch http://127.0.0.1:8000/... with a
     same-origin policy (Host would be the victim's hostname, not 127.0.0.1).

SECURITY (2026-09-01) - SETUP MODE fix ("no key set yet" state):
  Previously "LOCAL MODE" (zero real keys in .api_client_keys.json) let ANY loopback
  caller — any user or browser tab on this PC — through with api_role="admin" and no
  credentials at all. That is now impossible: while no real key exists, the middleware
  rejects every non-public request with an actionable 401 EXCEPT POST /api/auth/set-key
  (loopback-only), which writes the first key into .api_client_keys.json. WebSocket
  connections are refused in setup mode too. Mode is checked PER REQUEST/CONNECTION so
  setting a key via the endpoint unlocks everything immediately — no restart needed.
"""

import logging
from typing import Optional

from fastapi import WebSocket
from fastapi.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from app.keys import (
    is_api_key_valid,
    get_api_keys,
    get_key_role,
)

# SECURITY (2026-10-05): the ONLY endpoints that accept short-lived media tokens (?t=).
from app.media_tokens import MEDIA_TOKEN_PATHS

logger = logging.getLogger("COOLEMS")

# ---------- Public paths (no authentication required) ----------

PUBLIC_PATHS = {"/", "/favicon.ico"}

# SECURITY (2026-08-25): /files/ removed from public prefixes. File content is now
# only reachable through the authenticated /api/download endpoint (see auth middleware).
PUBLIC_PREFIXES = (
    "/static/",  # Static assets (UI shell files)
)

PUBLIC_EXACT = set()

# SETUP MODE (2026-09-01): the ONE path that works before any API key exists.
# Loopback-only in EVERY mode (see APIMiddleware.__call__ and _is_localhost_client).
SETUP_KEY_PATH = "/api/auth/set-key"

# (2026-10-07 setup-mode UI fix): the ONE extra read-only path that stays reachable while
# the CLIENT is still booting / waiting for a key. It exposes no credential - only the
# bootstrap phase and whether any key source exists yet - so the Settings modal can show
# live status instead of a dead 401 wall on every other /api/* path.
# NOTE: /api/setup/status is ALREADY TAKEN by routers/downloads.py (tool setup progress);
# this boot-status route lives at /api/setup/boot to avoid shadowing it.
SETUP_STATUS_PATH = "/api/setup/boot"


def _is_public_path(path: str) -> bool:
    """Return True if the path does not require authentication."""
    if path in PUBLIC_PATHS:
        return True
    if path in PUBLIC_EXACT:
        return True
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _is_local_mode() -> bool:
    """Check if running without any real API key (SETUP MODE).

    Checked PER REQUEST on purpose (2026-09-01): a key can be added at runtime via
    POST /api/auth/set-key and the very next request must already run KEYED MODE.
    The file read is cheap; keyed mode re-reads it per request anyway.
    """
    try:
        keys = get_api_keys()
        return len(keys) == 0
    except Exception:
        return True


def _is_loopback_host(host: str) -> bool:
        """Check whether *host* is a localhost/loopback address (stdlib only).

        The full SSRF defense module lives ONLY on the SERVER and reaches this CLIENT
        at runtime as in-memory code ('tools.ssrf_defense', dropped when the process
        exits). Auth runs before any server delivery exists, so it keeps its own
        minimal loopback check here instead of importing a local ssrf_defense copy.
        """
        if host in ('127.0.0.1', '::1', 'localhost'):
            return True

        import ipaddress as _ipaddress

        # Literal IP address form.
        try:
            addr = _ipaddress.ip_address(host)
            return bool(addr.is_loopback)
        except ValueError:
            pass

        # Hostname form -- resolve and check every returned address.
        try:
            import socket as _socket
            for res in _socket.getaddrinfo(host, None, _socket.AF_UNSPEC, _socket.SOCK_STREAM):
                ip_str = res[4][0]
                try:
                    if _ipaddress.ip_address(ip_str).is_loopback:
                        return True
                except ValueError:
                    continue
        except OSError:
            pass

        return False


def _set_request_state(scope, **kwargs):
    """Set attributes on scope['state'] so FastAPI Request.state sees them."""
    state_dict = scope.setdefault("state", {})
    for k, v in kwargs.items():
        state_dict[k] = v


def _is_localhost_client(scope: dict) -> bool:
    """Check if the request originates from localhost/loopback.

    SECURITY (2026-08-25): now requires BOTH a loopback source address AND a
    loopback Host header. The Host check closes the DNS-rebinding hole: an attacker
    page served from another machine can make same-origin fetches to
    http://127.0.0.1:PORT/... (source IP = victim's own loopback) but the Host
    header will carry the hostname the browser was told, which is NOT loopback for a
    rebinding domain. A genuine local user typing 127.0.0.1/localhost in the address
    bar passes both checks.

    Reads the client host from ASGI scope and verifies it's a loopback address.
    Used to enforce local-only access when no API keys are configured.
    """
    try:
        client = scope.get("client")  # (host, port) tuple or None
        if not client:
            return False
        host = client[0]

        # Fast checks for common localhost forms
        if host in ('127.0.0.1', '::1', 'localhost'):
            pass
        else:
            # Full loopback validation via stdlib-only helper (see _is_loopback_host)
            if not _is_loopback_host(host):
                return False

        # Host header must ALSO be loopback (DNS-rebinding defense).
        headers = dict(scope.get("headers", {}))
        host_header = headers.get(b"host", b"").decode("utf-8", errors="replace")
        host_name = host_header.split(":")[0].strip().lower() or "localhost"
        return _is_loopback_host(host_name)
    except Exception:
        return False


# ---------- ASGI Middleware (CLIENT - minimal) ----------

class APIMiddleware:
    """
    Custom ASGI middleware for CLIENT-side API key authentication.

    SETUP MODE (2026-09-01): When 0 real API keys are configured, the whole API is
    locked down — every non-public request gets an actionable 401 and WebSockets are
    refused — EXCEPT POST /api/auth/set-key from loopback callers, which writes the
    first key. This closes the old hole where any local user/browser got role=admin
    (full file R/W + code execution) before a single key existed: in that state the
    only possible action is "set api key".

    KEYED MODE: Key must pass local validation (config file CLIENT/config/.api_client_keys.json /
    in-memory runtime key / COOLEMS_CLIENT_API_KEY env var - the config file holds exactly
    ONE row {email, date_acquired, key}, plaintext by design since 2026-10-08)
    + email header. Role comes from the SERVER's last auth_ok (in-memory cache,
    fail-closed "user" fallback) - permission enforcement (allowed models/tools,
    rate limits) happens on the SERVER.

    /api/auth/set-key stays loopback-available in KEYED MODE too so the owner can
    change the key from the UI; a local user could edit .api_client_keys.json on disk
    anyway, so this grants no new power (the NEW key is still validated by the SERVER
    on connect).

    Accepts keys via HEADER ONLY:
      - Authorization: Bearer <key>
      - X-API-Key: <key>

    Sub-resource exception (SECURITY 2026-10-05): /api/download and /api/open also accept
    ?t=<media token> - a short-lived, single-use, path-bound capability minted by the UI
    through POST /api/auth/media-token AFTER header auth passed. <img>/<iframe>/anchor tags
    cannot send headers; before this fix they carried the master key as ?api_key=...&email=...
    which exposed it in browser history, Referer headers and logging hops.
    Skips public paths (/, /static/). Everything else (/api/*, /files/*) requires auth.
    Reloads keys from file on every request.
    """

    def __init__(self, app):
        self.app = app
        # NOTE (2026-09-01): mode is NOT cached here — it is re-checked per request in
        # __call__ so a key added at runtime via /api/auth/set-key takes effect on the
        # very next request without a restart. The init-time check below only feeds the
        # startup log line.
        if _is_local_mode():
            logger.info("Auth middleware initialized in SETUP MODE (no API key configured yet - only POST %s available)", SETUP_KEY_PATH)
        else:
            logger.info("Auth middleware initialized (keys reloaded per-request)")

    async def __call__(self, scope, receive, send):
        """ASGI interface - only intercept HTTP requests."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Skip public paths entirely (/, /static/ only — see PUBLIC_PREFIXES note).
        # The UI shell stays reachable in setup mode so the user CAN open Settings
        # and set a key - that is the whole point of this fix.
        if _is_public_path(path):
            await self.app(scope, receive, send)
            return

        # ===== SETUP KEY ENDPOINT (2026-09-01) — loopback-only in EVERY mode =====
        # Before a key exists this is the ONLY thing that works; afterwards it lets
        # the owner change the key from Settings. Loopback + Host-header check makes
        # it unreachable for remote LAN hosts and rebinding domains alike.
        # ===== LIVE BOOT STATUS (2026-10-07) - read-only, no auth required =====
        # Lets the UI show what boot is doing while setup mode locks every other /api/*.
        if path == SETUP_STATUS_PATH and scope.get("method", "GET").upper() == "GET":
            await self.app(scope, receive, send)
            return

        if path == SETUP_KEY_PATH:
            if not _is_localhost_client(scope):
                client = scope.get("client")
                host = client[0] if client else "unknown"
                logger.warning(
                    f"Set-key rejected non-localhost request from {host} (Host header check failed)"
                )
                response = JSONResponse(
                    status_code=HTTP_403_FORBIDDEN,
                    content={"detail": "Setting the API key is only possible on this machine."},
                )
                await response(scope, receive, send)
                return

            _set_request_state(
                scope,
                api_key="setup",
                api_role="admin",   # loopback owner only; the endpoint just writes the key entry
                allowed_tools=None,
                allowed_models=None,
            )
            await self.app(scope, receive, send)
            return

        # ===== SETUP MODE (2026-09-01): no real API key configured yet =====
        # Only "set api key" is possible — everything else is rejected with an
        # actionable 401. This replaces the old LOCAL-MODE behavior where any
        # loopback caller passed through as admin before a single key existed.
        if _is_local_mode():
            if not _is_localhost_client(scope):
                client = scope.get("client")
                host = client[0] if client else "unknown"
                logger.warning(
                    f"Setup mode rejected non-localhost request from {host} to {path}"
                )
                response = JSONResponse(
                    status_code=HTTP_403_FORBIDDEN,
                    content={
                        "detail": (
                            "Server is in local-only setup mode. Remote access requires API key authentication."
                        )
                    },
                )
                await response(scope, receive, send)
                return

            logger.info(f"Setup mode blocked {path} until an API key is set")
            response = JSONResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                content={
                    "detail": (
                        "No API key configured yet. Open Settings and set your API key first - "
                        "nothing else is available until then."
                    )
                },
            )
            await response(scope, receive, send)
            return

        # ===== DEFERRED BOOT GATE (2026-10-07 setup-mode UI fix) =====
        # Setup mode just ended (a key now exists) but the background provider/agent boot
        # may still be fetching the framework from the SERVER. Routers would see
        # provider/agent = None in that window, so answer 503 "retry shortly" instead -
        # the UI's Connect flow simply retries and everything works once boot finishes.
        # EXEMPTION (2026-10-07): /api/settings must pass through even while boot is
        # pending - it only reads/writes settings.json (no provider/agent access), and the
        # user needs it to fix a wrong SERVER address while the background bootstrap keeps
        # retrying. The bootstrap re-reads get_server_address_list() fresh every attempt,
        # so a corrected address applies on the very next iteration - no restart needed.
        try:
            from app.keys import is_boot_pending as _boot_pending_check
            if _boot_pending_check():
                method = scope.get("method", "GET").upper()
                if not (path == "/api/settings" and method in ("GET", "POST")):
                    response = JSONResponse(
                        status_code=503,
                        content={"detail": "CLIENT still starting - please retry in a moment."},
                        headers={"Retry-After": "2"},
                    )
                    await response(scope, receive, send)
                    return
        except Exception:  # pragma: no cover - flag check must never break auth
            pass

        # Extract API key from headers
        headers = dict(scope.get("headers", {}))
        key = None

        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        if auth_header.lower().startswith("bearer "):
            key = auth_header[7:].strip()

        if not key:
            api_key_header = headers.get(b"x-api-key", b"").decode("utf-8", errors="replace")
            if api_key_header:
                key = api_key_header.strip()

        # SECURITY (2026-10-05): ?api_key=... is NO LONGER accepted anywhere. Sub-resource
        # URLs (<img>/<iframe>/anchor cannot send headers) now use short-lived, single-use,
        # path-bound capability tokens: the UI proves itself once via normal header auth
        # (POST /api/auth/media-token) and passes ?t=<token> on MEDIA_TOKEN_PATHS. The
        # master key therefore never appears in any URL - not in browser history, Referer
        # headers or logging hops (it used to, as ?api_key=... since 2026-08-25).
        # Token hits EARLY-RETURN below: they must never reach is_api_key_valid() /
        # note_presented_key(), so a leaked token can't fill the outbound-provider runtime
        # credential store.
        if not key:
            try:
                from urllib.parse import parse_qs as _parse_qs
                qs = dict(_parse_qs(scope.get("query_string", b"").decode("utf-8", errors="replace")))
                media_token = (qs.get("t") or [""])[0].strip()
                qpath = (qs.get("path") or qs.get("filename") or [""])[0]
            except Exception:
                media_token, qpath = "", ""

            if media_token and path in MEDIA_TOKEN_PATHS:
                from app.media_tokens import consume_token as _consume_media_token
                if _consume_media_token(media_token, qpath):
                    logger.info(f"Media token accepted for {qpath}")
                    # Sentinel state only - the real key never enters request.state here.
                    _set_request_state(
                        scope,
                        api_key="<media-token>",   # sentinel: display/logging, never a credential
                        api_role="user",           # media surfaces are read-only; SERVER enforces more
                        allowed_tools=None,
                        allowed_models=None,
                    )
                    await self.app(scope, receive, send)
                    return
                logger.warning(f"Media token rejected for {path} (expired/exhausted/path mismatch)")

        if not key:
            response = JSONResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication failed. Please check your credentials."},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        # Basic local key validation (real auth happens on SERVER via WebSocket)
        if not is_api_key_valid(key):
            response = JSONResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication failed. Please check your credentials."},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        # (2026-09-29) remember the UI-presented key IN MEMORY so outbound provider auth
        # (control channel / tool-request reconnects after boot) can use it. Never on disk.
        try:
            from app.keys import note_presented_key as _npk
            _npk(key)
        except Exception:
            pass
    
        # Email is MANDATORY for all authenticated requests - header ONLY.
        # SECURITY (2026-10-05): the ?email= query fallback was removed together with
        # ?api_key=: sub-resource URLs now use media tokens (?t=) which carry no identity,
        # so nothing legitimate needs email in a URL anymore.
        email_header = headers.get(b"x-user-email", b"").decode("utf-8", errors="replace")
        if not email_header:
            response = JSONResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication failed. Please check your credentials."},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        # Role from LOCAL key entry (display/logging only).
        # allowed_models / allowed_tools are NOT resolved locally — the SERVER
        # enforces them and already filters model lists + tool definitions.
        real_role = get_key_role(key) or "user"

        _set_request_state(
            scope,
            api_key=key,
            api_role=real_role,
            allowed_tools=None,   # Server enforces (tools_response is role-filtered)
            allowed_models=None,  # Server enforces (models lists are pre-filtered)
        )

        await self.app(scope, receive, send)


# ---------- WebSocket Authentication (CLIENT - minimal) ----------

async def authenticate_websocket(websocket: WebSocket, api_keys=None, email: str = "") -> tuple:
    """
    Validate API key on a WebSocket connection.

    CLIENT does NOT enforce max-connections or role-based restrictions locally.
    That is handled by SERVER via the coolems_client/coolems_server WebSocket protocol.

    SETUP MODE (2026-09-01): when no real key exists yet, NO chat is possible at all —
    not even from loopback. The only thing allowed before a key exists is setting one
    (POST /api/auth/set-key over HTTP). Checked per connection so the very next WS
    attempt after a key was set already runs KEYED MODE auth.

    Returns:
        (True, key, session_id) if authenticated
        (False, None, None) if rejected
    """
    # SETUP MODE: no real API key configured yet -> refuse every WebSocket connection.
    if _is_local_mode():
        client = websocket.client
        host = client.host if client else "unknown"

        is_localhost = False
        try:
            is_localhost = _is_loopback_host(host)
        except Exception:
            logger.debug("Non-critical exception caught at CLIENT/app/auth.py")

        # SECURITY (2026-08-25): Host header must also be loopback (DNS-rebinding defense).
        if is_localhost:
            try:
                host_header = websocket.headers.get("host", "")
                host_name = host_header.split(":")[0].strip().lower() or "localhost"
                if not _is_loopback_host(host_name):
                    is_localhost = False
            except Exception:
                pass

        logger.warning(f"Setup mode rejected WebSocket from {host} (no API key configured yet)")
        if is_localhost:
            await websocket.close(
                code=4001,
                reason="No API key configured yet. Set your API key in Settings first.",
            )
        else:
            await websocket.close(
                code=4003,
                reason="Server is in local-only setup mode. Remote access requires API key authentication.",
            )
        return False, None, None

    # Extract key from query params
    key = websocket.query_params.get("api_key", "").strip()

    if not email:
        email = websocket.query_params.get("email", "").strip()

    if not key:
        await websocket.close(code=4001, reason="Authentication failed. Please check your credentials.")
        logger.warning(f"WebSocket rejected: missing API key from {websocket.client.host if websocket.client else 'unknown'}")
        return False, None, None

    # Basic local validation (real auth happens on SERVER)
    if not is_api_key_valid(key):
        await websocket.close(code=4001, reason="Authentication failed. Please check your credentials.")
        logger.warning(f"WebSocket rejected: invalid API key from {websocket.client.host if websocket.client else 'unknown'}")
        return False, None, None

    # (2026-09-29) remember the UI-presented key IN MEMORY for outbound provider auth.
    try:
        from app.keys import note_presented_key as _npk
        _npk(key)
    except Exception:
        pass
    
    # Email is MANDATORY
    if not email:
        await websocket.close(code=4001, reason="Authentication failed. Please check your credentials.")
        logger.warning(f"WebSocket rejected: missing email from {websocket.client.host if websocket.client else 'unknown'}")
        return False, None, None

    # Role from LOCAL key entry (display/logging only).
    # Permission enforcement happens on the SERVER — it filters model lists and
    # tool definitions before they ever reach this client.
    real_role = get_key_role(key) or "user"

    import time as _time
    import uuid as _uuid
    session_id = str(_uuid.uuid4())

    websocket.state.session_id = session_id
    websocket.state.api_key = key
    websocket.state.api_role = real_role
    websocket.state.allowed_models = None  # Server enforces (pre-filtered lists)
    websocket.state.allowed_tools = None   # Server enforces (role-filtered tools_response)
    websocket.state.connected_at = _time.time()

    logger.info("WebSocket authenticated successfully")
    return True, key, session_id


def release_websocket_session(key: str, session_id: Optional[str] = None, force: bool = False) -> None:
    """Release the WebSocket session for a key (call on disconnect)."""
    if key == "local":
        return
    logger.debug("Released WebSocket session")
