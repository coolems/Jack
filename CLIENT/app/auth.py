"""COOLEMS CLIENT - Authentication Module (CLIENT-SIDE ONLY)
CLIENT does NOT validate API keys or manage permissions locally. All authorization happens
on SERVER via WebSocket (configurable port, default 8080).
This module provides minimal HTTP middleware for local-mode detection only:
  - SETUP MODE (no real key configured yet): EVERYTHING is locked down except the
    loopback-only POST /api/auth/set-key endpoint that writes the first key. No chat, no
    files, no code execution before a key exists
  - KEYED MODE: a real key must be available locally (CLIENT/config/.api_client_keys.json,
    COOLEMS_CLIENT_API_KEY env var, or the in-memory runtime key from this UI session) so we
    know which credential to present to the SERVER + email header mandatory.
REAL auth: CLIENT sends email + API key to SERVER -> SERVER validates against its own
config/.api_keys.json and applies restrictions from profiles.json. The server's auth_ok /
tools_response messages carry the authoritative role, allowed_models and allowed_tools — the
CLIENT never resolves permissions from disk locally.
"""

import ipaddress
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
# (2026-10-15 licensing plan v12): read-only license status for the persistent badge.
# Public like /api/setup/boot: it exposes NO credential — only the local install choice
# and the cached server answer (badge label/tooltip). There are NO write endpoints any
# more: licensing is configured in ZZZ_initial_init.bat, and the UI badge is display-only.
LICENSE_STATUS_PATH = "/api/license/status"



def _is_public_path(path: str) -> bool:
    """True when the path needs no authentication."""
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

    Checked PER REQUEST on purpose: a key can be added at runtime via
    POST /api/auth/set-key and the very next request must already run KEYED MODE.
    The file read is cheap; keyed mode re-reads it per request anyway.
    """
    try:
        keys = get_api_keys()
        return len(keys) == 0
    except Exception:
        return True


# Exact spellings that mean "this machine". A hostname is attacker-chosen, so it is
# matched literally and NEVER resolved: a domain whose A record is 127.0.0.1 must not
# pass as local. Whole 127.0.0.0/8 and ::1 count as loopback by literal IP form.
_LOCAL_HOST_NAMES = frozenset(("127.0.0.1", "::1", "localhost"))


def _strip_host_port(value: str) -> str:
    """Bare lowercase hostname from a Host value, brackets and port removed."""
    value = (value or "").strip().lower()
    if value.startswith("["):  # IPv6 literal: [::1]:8000
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if value.count(":") == 1:  # IPv4 or hostname with port
        return value.split(":")[0]
    return value


def _is_loopback_host(host: str) -> bool:
    """True when *host* is a literal loopback address or name (stdlib only, no DNS).

    The full SSRF defense module lives ONLY on the SERVER and reaches this CLIENT at
    runtime as in-memory code, so auth keeps its own minimal check here.
    """
    host = _strip_host_port(host)
    if host in _LOCAL_HOST_NAMES:
        return True
    try:
        return bool(ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def _is_local_host_header(raw_host: str) -> bool:
    """True when a Host header literally names this machine; absent or blank -> False."""
    return bool(_strip_host_port(raw_host)) and _is_loopback_host(raw_host)


def _set_request_state(scope, **kwargs):
    """Set attributes on scope['state'] so FastAPI Request.state sees them."""
    state_dict = scope.setdefault("state", {})
    for k, v in kwargs.items():
        state_dict[k] = v


def _is_localhost_client(scope: dict) -> bool:
    """True when the request comes from this machine and targets it.

    Requires BOTH a loopback source address AND a loopback Host header. The Host check is
    the DNS-rebinding defense: a page on another machine can fetch http://127.0.0.1:PORT/
    with the victim's own loopback as source IP, but its Host header carries the hostname
    the browser was told. That name is matched literally only - never resolved - so a
    domain pointing at 127.0.0.1 fails. A missing Host header also fails: a client that
    cannot say which host it meant is not proven local.
    """
    try:
        client = scope.get("client")  # (host, port) tuple or None
        if not client:
            return False
        if not _is_loopback_host(client[0]):
            return False

        headers = dict(scope.get("headers", {}))
        return _is_local_host_header(headers.get(b"host", b"").decode("utf-8", errors="replace"))
    except Exception:
        return False


# ---------- ASGI Middleware (CLIENT - minimal) ----------

class APIMiddleware:
    """ASGI middleware for CLIENT-side API key authentication.

    SETUP MODE: with 0 real API keys configured the whole API is locked down - every
    non-public request gets an actionable 401 and WebSockets are refused - EXCEPT
    POST /api/auth/set-key from loopback callers, which writes the first key. Before a key
    exists the only possible action is "set api key", so no local caller can obtain
    role=admin (file R/W + code execution) in that state.
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

        # ===== LICENSE STATUS (licensing plan v12) - read-only, no auth required =====
        # The badge is display-only: it shows the license info the server matched from
        # coolems.com. No forms/registration in the UI — that happens at init time.
        if path == LICENSE_STATUS_PATH and scope.get("method", "GET").upper() == "GET":
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
    """Validate API key on a WebSocket connection.

    CLIENT does NOT enforce max-connections or role-based restrictions locally.
    That is handled by SERVER via the coolems_client/coolems_server WebSocket protocol.

    SETUP MODE: when no real key exists yet, NO chat is possible at all —
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
            is_localhost = _is_loopback_host(host) and _is_local_host_header(
                websocket.headers.get("host", "")
            )
        except Exception:
            logger.debug("Non-critical exception caught at CLIENT/app/auth.py")

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
