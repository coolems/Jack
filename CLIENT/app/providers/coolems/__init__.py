"""COOLEMS distributed provider protocol (CLIENT side).

Shared helpers for WebSocket relay and direct connections.
API key loading (2026-08-23): the key always comes from app.keys.load_coolems_api_key(),
which reads <CLIENT>/config/.api_client_keys.json (renamed from .api_keys.json so it is
not confused with the SERVER's own config/.api_keys.json).

TRANSPORT REALITY (2026-08-20 doc fix): the wire protocol is WebSocket TEXT frames
carrying JSON objects -- there is NO binary length-prefix framing. The old
send_frame()/recv_frame() helpers were dead code and have been removed along with
the misleading "Binary framing" docstring. See SERVER's app/providers/coolems/__init__.py
for the full message-type reference (auth, request/content/done, health_check,
models_request, current_model, model_switch, tools_request, tool_code_request).

PROTOCOL VERSIONING (2026-08-20): every auth frame carries "protocol_version"
(config.PROTOCOL_VERSION) and the client verifies the value echoed back in auth_ok
via _check_protocol(). A missing echo means a legacy v1 server -- tolerated for one
release cycle with a loud warning; an explicit mismatch raises ConnectionError.

AUTH INFO APPLICATION (2026-08-20): apply_auth_info() is the ONE place where
auth_ok data mutates client config globals (context_window -> CONTEXT_WINDOW_TOKENS).
Every auth path (direct, relay, control channel) calls it so the mutation happens in
exactly one code location with one log line.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from config import PROTOCOL_VERSION as _CLIENT_PROTOCOL_VERSION

logger = logging.getLogger("COOLEMS.CoolemsProtocol")


# ─── Protocol version check (shared by all auth paths) ──

def _check_protocol(auth_response: dict, context: str = "") -> None:
    """Verify the protocol_version echoed back in auth_ok matches ours.

    Called after EVERY successful auth handshake (direct/relay/control channel).
      - echo == our version  -> OK (silent)
      - echo missing         -> legacy v1 server: loud warning, tolerated one cycle
      - explicit mismatch    -> ConnectionError with an actionable message
    """
    if not isinstance(auth_response, dict):
        return
    echoed = auth_response.get("protocol_version")
    where = f" ({context})" if context else ""
    if echoed is None:
        logger.warning(
            f"[CLIENT] SERVER{where} did not echo protocol_version (legacy v1 server). "
            f"Client runs PROTOCOL_VERSION={_CLIENT_PROTOCOL_VERSION}. Upgrade the SERVER."
        )
        return
    try:
        echoed = int(echoed)
    except (TypeError, ValueError):
        logger.warning(f"[CLIENT] SERVER{where} sent invalid protocol_version={echoed!r}")
        return
    if echoed != _CLIENT_PROTOCOL_VERSION:
        raise ConnectionError(
            f"Protocol version mismatch{where}: CLIENT speaks v{_CLIENT_PROTOCOL_VERSION}, "
            f"SERVER speaks v{echoed}. Update the {'SERVER' if echoed < _CLIENT_PROTOCOL_VERSION else 'CLIENT'} "
            f"so both ends agree."
        )


def apply_auth_info(auth_response: dict, context: str = "") -> None:
    """Apply auth_ok data to client config. THE single mutation point (2026-08-20).

    Currently: context_window from SERVER becomes the live CONTEXT_WINDOW_TOKENS so
    history trimming and token budgets use the model's REAL window, not a local guess.
    Adding new auth-derived fields later means editing exactly this function.
    """
    if not isinstance(auth_response, dict):
        return
    server_context_window = auth_response.get("context_window")
    if server_context_window is not None:
        import config.config as _cfg
        _cfg.CONTEXT_WINDOW_TOKENS = int(server_context_window)
        where = f" via {context}" if context else ""
        logger.info(f"[CLIENT] Received context_window={server_context_window} from SERVER{where}")

        # (2026-09-29) authoritative role/email cache - the CLIENT keeps no identity on disk.
        # The SERVER's auth_ok is the single source of truth; every consumer reads it via
        # app.keys.get_key_role() / get_key_email().
        try:
            import app.keys as _keys_mod
            role = auth_response.get("role")
            if isinstance(role, str) and role.strip():
                _keys_mod._server_role = role.strip()
            email = auth_response.get("email")
            if isinstance(email, str) and email.strip():
                _keys_mod._server_email = email.strip()
        except Exception as e:  # pragma: no cover - defensive; never break the auth path
            logger.debug("[CLIENT] Could not cache auth_ok role/email: %s", e)
    

# ─── WebSocket Relay Helpers ──

def _parse_web_relay_address(addr, default_port):
    """Parse host:port from address string."""
    if addr.startswith("["):
        bracket_end = addr.find("]")
        if bracket_end == -1:
            raise ValueError(f"Invalid IPv6 address (missing closing bracket): {addr}")
        host = addr[1:bracket_end]
        port_str = addr[bracket_end + 1:]
        if port_str.startswith(":"):
            port = int(port_str[1:])
        else:
            port = default_port
    elif ":" in addr:
        host, port = addr.rsplit(":", 1)
        port = int(port)
    else:
        host = addr
        port = default_port
    return host, port


def _build_websocket_url(host, port, use_ssl, path=""):
    """Build WebSocket URL with proper scheme (ws:// or wss://).

    *path* is an optional route suffix for the VPS relay router ("/ws/client");
    must start with "/" when given.
    """
    scheme = "wss" if use_ssl else "ws"
    suffix = ""
    if path:
        p = str(path)
        if not p.startswith("/"):
            p = "/" + p
        suffix = p.rstrip("/") or "/"
    if ":" in str(host):
        return f"{scheme}://[{host}]:{port}{suffix}"
    return f"{scheme}://{host}:{port}{suffix}"

class NoServerReachable(ConnectionError):
    """Every configured server address failed to accept a connection."""


# --- Sticky "known-good" server memory (2026-09-01) -------------------------
# Old behavior: every single connect attempt re-scanned the ENTIRE address list from
# index 0. New behavior: once an address has answered, it is remembered here
# (in-process) AND persisted to settings.json ('server_address' via
# config._remember_good_server). Every later connection goes straight to that
# known-good address and only falls back to a full scan when THAT address actually
# dies. The full list is asked again exactly at that moment, and whichever answers
# becomes the new sticky one - then we use it until IT dies, and so on.

_sticky_url: str | None = None   # ws URL of the last confirmed-good candidate
_sticky_ctx = None               # its ssl context (always kept paired with the URL)
_sticky_lock = asyncio.Lock()    # guards sticky read/update races


def _set_sticky(ws_url, ctx):
    """Mark *ws_url* as the known-good server (memory + settings.json persistence)."""
    global _sticky_url, _sticky_ctx
    _sticky_url = ws_url
    _sticky_ctx = ctx
    try:
        from config import _remember_good_server
        # Persist the bare 'host[:port]' form exactly as settings.json stores it -
        # _build_websocket_url() produced this URL from that same string, so stripping
        # the scheme round-trips losslessly (incl. bracketed IPv6 '[::1]:8080').
        addr = ws_url.split("://", 1)[1] if "://" in ws_url else ws_url
        _remember_good_server(addr)
    except Exception:
        pass  # persistence is best-effort; in-process stickiness still works


def _clear_sticky():
    """Forget the known-good server (all addresses failed)."""
    global _sticky_url, _sticky_ctx
    _sticky_url = None
    _sticky_ctx = None


async def open_with_failover(candidates, *, open_timeout: float = 10.0, **connect_kwargs):
    """Connect to the FIRST reachable candidate; return (websocket, ws_url).

    candidates: ordered list of (ws_url, ssl_context) - index 0 is tried first
                (put 'localhost' there so local always wins), each next address is
                only attempted when the previous one cannot be reached.

    STICKY BEHAVIOR (2026-09-01): if a known-good candidate from an earlier successful
    connection is still present in *candidates*, it is tried FIRST and - while it keeps
    answering - the rest of the list is never touched at all. The full scan happens only
    when there is no sticky memory yet, or when the sticky address fails (it died). Any
    successful connect becomes the new sticky candidate; when EVERY candidate fails the
    sticky memory is cleared and NoServerReachable is raised.

    Every attempt is bounded by open_timeout so one dead/hung address cannot stall
    startup; raises NoServerReachable when every candidate fails.
    """
    import websockets

    async def _try_one(ws_url, ctx):
        return await asyncio.wait_for(
            websockets.connect(ws_url, ssl=ctx if ctx else None, **connect_kwargs),
            timeout=open_timeout)

    # 1) Known-good address first (only while it is still in the fresh candidate list -
    #    a settings.json edit that removed an address invalidates the memory naturally).
    async with _sticky_lock:
        sticky = (_sticky_url, _sticky_ctx) if _sticky_url else None
        if sticky and sticky[0] not in [u for u, _ in candidates]:
            _clear_sticky()  # configured away - drop stale memory
            sticky = None
    skip_url = None  # address proven dead THIS round - do not burn a second attempt on it
    errors = []      # human-readable record of every failed address this round
    if sticky:
        try:
            ws = await _try_one(*sticky)
        except Exception as e:
            logger.warning(f"[CLIENT] Known-good address {sticky[0]} is down ({type(e).__name__}: {e}) - scanning full list")
            skip_url = sticky[0]
            errors.append(f"{skip_url} ({type(e).__name__})")  # keep the NoServerReachable message complete
        else:
            logger.info(f"[CLIENT] Connected to Direct WS at {sticky[0]}")
            return ws, sticky[0]

    # 2) Full scan (first boot / no memory yet, or the known-good one just died).
    for ws_url, ctx in candidates:
        if ws_url == skip_url:  # already failed as sticky this round
            logger.debug(f"[CLIENT] Skipping {ws_url} - just failed as known-good address")
            continue
        try:
            ws = await _try_one(ws_url, ctx)
        except Exception as e:
            logger.warning(f"[CLIENT] Address {ws_url} not reachable ({type(e).__name__}: {e}) - trying next")
            errors.append(f"{ws_url} ({type(e).__name__})")
            continue
        async with _sticky_lock:
            prev = _sticky_url
            _set_sticky(ws_url, ctx)
        # Exactly ONE info line per successful connect - the endpoint we actually reached.
        logger.info(f"[CLIENT] Connected to Direct WS at {ws_url}")
        if prev != ws_url:  # address change is a state transition (verbose detail kept at debug)
            logger.debug(f"[CLIENT] Known-good server changed: {prev or 'none'} -> {ws_url} (remembered until it dies)")
        return ws, ws_url

    # 3) Nothing answered at all - forget the dead sticky address so the next round
    #    does not waste its first attempt on a server we just proved is down.
    async with _sticky_lock:
        if _sticky_url:
            logger.info(f"[CLIENT] All server addresses failed - clearing known-good memory ({_sticky_url})")
        _clear_sticky()
    raise NoServerReachable("no configured server address answered; tried " + ", ".join(errors))


@asynccontextmanager
async def connect_with_failover(candidates, *, open_timeout: float = 10.0, **connect_kwargs):
    """Context-manager variant of open_with_failover for scoped connections."""
    ws, _ws_url = await open_with_failover(candidates, open_timeout=open_timeout, **connect_kwargs)
    try:
        yield ws
    finally:
        try:
            await ws.close()
        except Exception:
            pass
