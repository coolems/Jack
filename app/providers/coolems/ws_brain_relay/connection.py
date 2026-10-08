"""WebSocket relay brain mode - connects to external web relay as a brain server.

Handles reconnection logic, auth with the relay, and message routing from relay clients
to the local LLM backend. The implementation is split across this package:

    connection.py      TLS setup (fail-closed), gate key, connect + reconnect loop
    request_handler.py per-relayed-client chat-request pipeline (auth -> rate limit ->
                       model authorization -> stop propagation -> queue enqueue)
    relay_handlers.py  _RelayTargetAdapter, _relay_send and the model_switch / tools /
                       tool_code message handlers (keyed on the RELAYED client's profile)

The flat app/providers/coolems/ws_brain_relay.py file is a thin re-export facade so
existing imports ('from .ws_brain_relay import connect_to_web_relay', '_RelayTargetAdapter')
keep working unchanged.
"""

import asyncio
import json
import logging

from ..connection_enforcement import conn_tracker, rate_limiter
# (2026-08-20) relay-mode stop propagation: same owner-scoped tracker the direct path uses.
from ..ws_client_handler import _ActiveConversations, _filter_models_by_user
# (2026-08-23 dedupe) Single source of truth for model authorization:
# exact folder-name matching only - no normalization, no substring.
from app.auth_gateway.model_resolver import is_model_allowed

# One tracker for the brain<->relay link. Keys are (relay_client_id, conv_id): every
# client behind the relay is a distinct 'connection' as far as stop events go.
# (2026-10-07 split) module-level singleton restored here - it lived at monolith top level
# before the package split and must stay shared with request_handler._handle_relay_request.
_relay_convs = _ActiveConversations()

from .request_handler import _handle_relay_request  # per-relayed-client request pipeline
# (2026-10-07 split) the message loop below still calls these; they live in relay_handlers.py.
# noqa: E402 - must come after _relay_convs so request_handler's back-import resolves.
from .relay_handlers import (  # noqa: E402
    _handle_relay_model_switch,
    _handle_relay_tool_code_request,
    _handle_relay_tools_request,
    _relay_send,
    _resolve_relay_client_user_info,
)

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")

async def connect_to_web_relay(server_provider):

    """Connect to the web relay server as a brain server. Runs as asyncio.Task on main loop."""

    import websockets

    from config import (

        WEB_RELAY_MAX_MESSAGE_SIZE,

        WEB_RELAY_HEARTBEAT_INTERVAL,

        WEB_RELAY_HEARTBEAT_TIMEOUT,

        WS_HANDSHAKE_TIMEOUT,

        WEB_RELAY_VERIFY_SSL_CERTS,

        # (2026-09-01 S3) optional certificate pin for the brain->relay link
        WEB_RELAY_CERT_PIN_FINGERPRINT,

        COOLEMS_RECONNECT_MIN_DELAY,

        COOLEMS_RECONNECT_MAX_DELAY,

        USE_WEB_SERVER,
        PROTOCOL_VERSION,

        PROFILE_DEFAULT_RATE_LIMIT,

        WEB_RELAY_DEFAULT_PORT,

    )

    from .. import _parse_web_relay_address, _build_websocket_url



    # Get actual context window from server config (SERVER is the source of truth)

    from config import CONTEXT_WINDOW_TOKENS as SERVER_CONTEXT_WINDOW



    host, port = _parse_web_relay_address(USE_WEB_SERVER, WEB_RELAY_DEFAULT_PORT)

    use_ssl = True  # TLS always enabled

    ws_url = _build_websocket_url(host, port, use_ssl, "/ws/brain")



    ssl_context = None

    # (2026-09-23 v4) FAIL-CLOSED TLS for the internet-facing brain<->relay link.
    # The relay is a PUBLIC host: either full CA verification (Let's Encrypt etc.)
    # or an exact cert pin must be configured. There is NO silent CERT_NONE fallback
    # anymore - an untrusted transport would expose the brain's admin API key in the
    # first auth frame, so we refuse to connect instead.

    if use_ssl and WEB_RELAY_VERIFY_SSL_CERTS:

        # Full CA verification (relay behind a public CA, e.g. Let's Encrypt).
        import ssl as ssl_mod

        ssl_context = ssl_mod.create_default_context()

    elif use_ssl and WEB_RELAY_CERT_PIN_FINGERPRINT:

        # (2026-09-01 S3) certificate PINNING: TLS still encrypts, but trust is an exact
        # SHA-256 fingerprint of the relay's cert -- works for self-signed relays without a
        # CA chain and defeats MITM with any other certificate.
        from ..ssl_pinning import build_pinned_ssl_context

        ssl_context = build_pinned_ssl_context(WEB_RELAY_CERT_PIN_FINGERPRINT)

        if ssl_context is None:

            logger.warning("[SERVER] Brain relay: configured cert pin unusable (fix WEB_RELAY_CERT_PIN_FINGERPRINT) - "
                           "refusing to connect without trusted TLS")
            return

    elif use_ssl:

        # Neither CA verification nor a pin configured -> REFUSE (was: silent CERT_NONE).
        logger.error(
            "[SERVER] Brain relay TLS untrusted: set WEB_RELAY_VERIFY_SSL_CERTS=True (public CA) "
            "or WEB_RELAY_CERT_PIN_FINGERPRINT in config - refusing to connect without trusted TLS"
        )
        return

    # (2026-09-23 v5) The single-file pure-PHP web relay (web_server_relay/index.php)
    # validates the brain by IP lock + secret key BEFORE tunneling to the Python relay.
    # The brain's own admin API key doubles as that gate key and travels in the upgrade
    # URI query. TLS must be trusted (fail-closed above) so it never transits unencrypted;
    # a plain VPS relay without an edge gate simply ignores the extra query parameter.
    from .. import _load_coolems_api_key

    _gate_key = _load_coolems_api_key() or ""

    if _gate_key:

        from urllib.parse import quote

        ws_url += "?coolems_gate=" + quote(_gate_key, safe="")






    # (2026-09-23 v4) never log the full URL - it carries the gate key in the query string.
    _ws_url_display = ws_url.split("?")[0]
    logger.info(f"[SERVER] Connecting to web relay at {_ws_url_display} as brain...")



    retry_count = 0



    while server_provider._running:

        brain_conn_id = None  # Track connection ID for cleanup (Fix #2)

        client_relay_convs: set[tuple[str, str]] = set()  # (relay_client_id, conv_id) keys active on THIS relay link

        # (2026-09-01 S1 fix) per-link registry of relayed client_id -> profile name. Filled
        # (2026-09-23 v4) per-link registry of relayed client_id -> the client's API KEY.
        # Filled from the relay's 'client_connected' notification; used to authenticate each
        # relayed client against OUR OWN config/.api_keys.json - exactly like direct mode.
        # The role name the relay advertises is kept only for logs (advisory).
        _relay_client_keys: dict[str, str] = {}
        _relay_client_roles_advisory: dict[str, str] = {}  # log-only profile names from the relay


        try:

            async with websockets.connect(

                ws_url,

                max_size=WEB_RELAY_MAX_MESSAGE_SIZE,

                ping_interval=WEB_RELAY_HEARTBEAT_INTERVAL,

                ping_timeout=WEB_RELAY_HEARTBEAT_TIMEOUT,

                ssl=ssl_context if ssl_context else None,

            ) as ws:

                # Authenticate as brain server

                from .. import _load_coolems_api_key

                api_key = _load_coolems_api_key()



                if not api_key:

                    logger.error("[SERVER] No active API key found for web relay auth")

                    return



                # Send auth message (context_window is NOT here — it goes in a

                # separate brain_info message after the relay confirms auth_ok,

                # so the relay can forward it to clients' auth_ok).

                await ws.send(json.dumps({

                    "type": "auth",

                    "api_key": api_key,

                    "role_type": "brain",
                    "protocol_version": PROTOCOL_VERSION,

                }))



                # Wait for auth response

                auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=WS_HANDSHAKE_TIMEOUT))

                if auth_response.get("type") != "auth_ok":

                    logger.error(f"[SERVER] Web relay auth failed: {auth_response}")

                    return

                # (2026-09-23 v4) verify the protocol version echoed by the relay - same
                # contract as direct mode: missing echo = legacy relay (loud warning, tolerated
                # one cycle); explicit mismatch = incompatible relay, refuse to continue.
                _echoed_proto = auth_response.get("protocol_version")
                if _echoed_proto is None:
                    logger.warning("[SERVER] Web relay did not echo protocol_version (legacy v3 relay). "
                                   f"Brain runs PROTOCOL_VERSION={PROTOCOL_VERSION}. Upgrade the relay.")
                else:
                    try:
                        _echoed_proto = int(_echoed_proto)
                    except (TypeError, ValueError):
                        logger.error(f"[SERVER] Web relay sent invalid protocol_version={_echoed_proto!r} - refusing")
                        return
                    if _echoed_proto != PROTOCOL_VERSION:
                        logger.error(f"[SERVER] Protocol mismatch: brain v{PROTOCOL_VERSION}, relay v{_echoed_proto} "
                                       f"- update the {"relay" if _echoed_proto < PROTOCOL_VERSION else "brain"} so both ends agree")
                        return



                # Authenticate brain with real profile permissions

                brain_user_info = server_provider._auth_gateway.authenticate(api_key)

                if not brain_user_info:

                    logger.error("[SERVER] Brain API key has no valid profile in auth gateway - refusing to connect")

                    return

                ws._user_info = brain_user_info



                # --- Connection limit enforcement for brain relay (Fix #2) ---

                max_connections = brain_user_info.get("max_connections", 1)

                if not await conn_tracker.can_connect(api_key, max_connections):

                    current_count = await conn_tracker.get_active_count(api_key)

                    logger.warning(

                        f"[SERVER] Brain relay connection REJECTED - "

                        f"key={api_key[:8]}... already has {current_count}/{max_connections} connections"

                    )

                    await ws.send(json.dumps({

                        "type": "error",

                        "message": (

                            f"Connection limit reached ({current_count}/{max_connections}). "

                            f"Close existing connections or upgrade your profile."

                        )

                    }))

                    server_provider._ws_connected = False

                    # Exponential backoff before retrying to avoid tight loop (Fix #2-b)

                    if server_provider._running:

                        delay = min(

                            COOLEMS_RECONNECT_MIN_DELAY * (2 ** retry_count),

                            COOLEMS_RECONNECT_MAX_DELAY

                        )

                        logger.info(f"[SERVER] Retrying brain relay connection in {delay}s...")

                        await asyncio.sleep(delay)



                    continue  # Skip this connection attempt, retry loop will reconnect



                # (2026-09-23 v4) expose this link's client registries on the socket so
                # _resolve_relay_client_user_info() can authenticate relayed clients by KEY.
                ws._relay_client_keys = _relay_client_keys
                ws._relay_client_roles_advisory = _relay_client_roles_advisory

                # Register the brain relay connection in tracker

                brain_conn_id = await conn_tracker.register(api_key, ws)

                if not brain_conn_id:

                    logger.error("[SERVER] Failed to register brain relay connection")

                    server_provider._ws_connected = False

                    continue



                # Start rate limiter cleanup task (lazy start)

                await rate_limiter.start_cleanup()



                max_rate = brain_user_info.get("max_rate_limit", PROFILE_DEFAULT_RATE_LIMIT)



                logger.info(f"[SERVER] Connected to web relay as brain server (role={brain_user_info['role']})")



                # Send model context_window to the relay NOW that auth is confirmed.

                # The SERVER is the single source of truth for this value; the relay

                # stores it on the brain entry and merges it into every client's

                # auth_ok so CLIENTs receive the real context window through a relay.

                await ws.send(json.dumps({

                    "type": "brain_info",

                    "context_window": SERVER_CONTEXT_WINDOW,

                }))



                server_provider._ws_connected = True

                retry_count = 0



                # Main message loop - receive client requests via relay

                try:

                    async for raw_msg in ws:

                        if not server_provider._running:

                            break



                        msg = json.loads(raw_msg)

                        msg_type = msg.get("type", "")



                        if msg_type == "request":
                            # Per-relayed-client pipeline (auth -> rate limit -> model authz
                            # -> stop propagation -> queue enqueue): see request_handler.py.
                            await _handle_relay_request(ws, server_provider, msg, client_relay_convs)
                        elif msg_type == "health_check":
                            logger.info(f"[SERVER<-RELAY] Health check via relay (client={msg.get('_relay_from', '?')})")
                            local = server_provider._get_local_provider()
                            ok, err, models = await local.health_check(force=False)
                            # (2026-09-01 S1 fix) filter the model list by the RELAYED CLIENT's
                            # profile before it leaves the brain; unknown client -> empty list.
                            _hc_user_info = await _resolve_relay_client_user_info(ws, msg, server_provider)
                            if _hc_user_info:
                                filtered_hc = _filter_models_by_user([{"name": m} for m in models], _hc_user_info, server_provider)
                                out_models = [{"name": m["name"]} for m in filtered_hc]
                            else:
                                out_models = []
                            await _relay_send(ws, {
                                "type": "health_ok" if ok else "error",
                                "models": out_models,
                                "message": err
                            }, msg.get("_relay_from") or "")



                        elif msg_type == "models_request":
                            logger.info(f"[SERVER<-RELAY] Models request via relay (client={msg.get('_relay_from', '?')})")
                            local = server_provider._get_local_provider()
                            models_list = await local.get_models_list()
                            # (2026-09-01 S1 fix) filter by the RELAYED CLIENT's profile before
                            # the list leaves the brain; unknown client -> empty list.
                            _mr_user_info = await _resolve_relay_client_user_info(ws, msg, server_provider)
                            if _mr_user_info:
                                models_list = _filter_models_by_user(models_list, _mr_user_info, server_provider)
                            else:
                                models_list = []
                            await _relay_send(ws, {
                                "type": "models_response",
                                "models": models_list
                            }, msg.get("_relay_from") or "")



                        elif msg_type == "current_model":
                            # (2026-08-23 fix) force=True bypasses the fresh model-state cache
                            logger.info(f"[SERVER<-RELAY] Current model request via relay (client={msg.get('_relay_from', '?')})")
                            local = server_provider._get_local_provider()
                            current = (await local.get_current_model(force=bool(msg.get("force")))) or ""
                            # (2026-09-01 S1 fix) only reveal the loaded model when the RELAYED
                            # CLIENT's profile may use it; unknown client -> empty string.
                            _cm_user_info = await _resolve_relay_client_user_info(ws, msg, server_provider)
                            if not (_cm_user_info and is_model_allowed(_cm_user_info, current)):
                                current = ""
                            await _relay_send(ws, {
                                "type": "current_model_response",
                                "model": current
                            }, msg.get("_relay_from") or "")



                        elif msg_type == "model_switch":

                            model_filename = msg.get("model", "")

                            logger.info(f"[SERVER<-RELAY] Model switch request via relay: {model_filename} (client={msg.get('_relay_from', '?')})")

                            # (2026-09-01 S1 fix) msg carries _relay_from so the handler can check

                            # the RELAYED CLIENT's model permissions, not the brain's.

                            await _handle_relay_model_switch(ws, server_provider, model_filename, msg)



                        elif msg_type == "stop":

                            # (2026-08-20) relayed user-stop: abort generation on the brain side.

                            _relay_client_id = msg.get("_relay_from") or "unknown"

                            _conv_id = msg.get("conv_id", "")

                            # (2026-09-08 multi-chat) also drop this chat's QUEUED request(s)
                            # from the backlog so workers don't pop work nobody is listening to.
                            try:
                                from app.server_queue import get_queue

                                _q = get_queue()
                                if _q is not None:
                                    await _q.cancel_owner(f"relay:{_relay_client_id}|{_conv_id}")
                            except Exception as e:  # pragma: no cover - cleanup must never raise
                                logger.warning(f"[SERVER] Queue cancel on relay stop failed: {e}")

                            if _relay_convs.unregister_and_stop(_relay_client_id, _conv_id):
                                client_relay_convs.discard((_relay_client_id, _conv_id))

                                logger.info(f"[SERVER<-RELAY] Stop propagated for relayed client {_relay_client_id} conv={_conv_id}")



                        elif msg_type == "client_connected":
                            # (2026-09-23 v4) the relay tells us a client joined this brain link and
                            # forwards that client's API key + email. We authenticate it against OUR OWN
                            # config/.api_keys.json - same logic as direct mode: role/email come from
                            # server-side key lookup, never from a name the relay (or client) sends.
                            _new_client = msg.get("client_id", "")
                            if _new_client:
                                _relay_client_keys[_new_client] = msg.get("client_api_key") or ""
                                _relay_client_roles_advisory[_new_client] = msg.get("client_role") or ""
                                logger.info("[SERVER<-RELAY] Client %s joined (advisory role=%r, email=%r) "
                                           "- brain will authenticate by key", _new_client,
                                           _relay_client_roles_advisory[_new_client], msg.get("client_email") or "?")

                        elif msg_type == "client_disconnected":

                            # (2026-08-20) the relay told us one of its clients vanished --

                            # abort everything that client had in flight on this brain.

                            _gone_client = msg.get("client_id", "")

                            _relay_convs.stop_all_for_client(

                                {k for k in list(client_relay_convs) if k[0] == _gone_client}

                            )

                            # (2026-09-08 multi-chat) remove this vanished client's QUEUED requests too
                            # (running ones are already aborted via stop_all_for_client above).
                            try:
                                from app.server_queue import get_queue

                                _q = get_queue()
                                if _q is not None:
                                    for (_cid, _cvid) in [k for k in client_relay_convs if k[0] == _gone_client]:
                                        await _q.cancel_owner(f"relay:{_cid}|{_cvid}")
                            except Exception as e:  # pragma: no cover - cleanup must never raise
                                logger.warning(f"[SERVER] Queue cancel on relay disconnect failed: {e}")
                            # (2026-09-01 B6 fix) in-place mutation instead of rebind -- matches the direct path's
                            # 'do NOT rebind client_convs' contract so finally-block references stay stable.
                            for _k in [k for k in client_relay_convs if k[0] == _gone_client]:
                                client_relay_convs.discard(_k)
                            _relay_client_keys.pop(_gone_client, None)          # (2026-09-23 v4) drop key registry entry
                            _relay_client_roles_advisory.pop(_gone_client, None)  # advisory log-only names too

                        elif msg_type == "tools_request":
                            logger.info(f"[SERVER<-RELAY] Tools request via relay (client={msg.get('_relay_from', '?')})")
                            # (2026-09-01 S1 fix) resolve the RELAYED CLIENT's role, not the brain socket's
                            # own user info attribute: that holds the BRAIN's profile and would grant every
                            # client behind the relay the brain's (admin) tool privileges.
                            await _handle_relay_tools_request(ws, msg, server_provider)

                        elif msg_type == "tool_code_request":
                            logger.info(f"[SERVER<-RELAY] Tool code request via relay: name={msg.get('name', '?')} (client={msg.get('_relay_from', '?')})")
                            await _handle_relay_tool_code_request(ws, msg, server_provider)
                finally:

                    # (2026-08-20) relay link gone -> abort any generation still in flight

                    # for clients behind it, then clear the per-link key set.

                    if client_relay_convs:

                        _relay_convs.stop_all_for_client(client_relay_convs)

                        client_relay_convs.clear()



                    # --- Cleanup brain relay connection tracking (Fix #2) ---

                    if brain_conn_id is not None:

                        await conn_tracker.unregister(brain_conn_id)

                        logger.info("[SERVER] Brain relay connection unregistered from tracker")



        except websockets.exceptions.ConnectionClosed as e:

            logger.warning(f"[SERVER] Web relay connection closed: {e}")

            if server_provider._running:

                retry_count += 1

                delay = min(COOLEMS_RECONNECT_MIN_DELAY * (2 ** retry_count), COOLEMS_RECONNECT_MAX_DELAY)

                logger.info(f"[SERVER] Reconnecting to web relay in {delay}s...")

                await asyncio.sleep(delay)



        except Exception as e:

            if isinstance(e, asyncio.CancelledError):

                return  # Clean shutdown

            logger.error(f"[SERVER] Error connecting to web relay: {e}", exc_info=True)

            if server_provider._running:

                retry_count += 1

                delay = min(COOLEMS_RECONNECT_MIN_DELAY * (2 ** retry_count), COOLEMS_RECONNECT_MAX_DELAY)

                logger.info(f"[SERVER] Reconnecting to web relay in {delay}s...")

                await asyncio.sleep(delay)



    server_provider._ws_connected = False
