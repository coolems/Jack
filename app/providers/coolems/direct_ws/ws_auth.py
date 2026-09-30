"""Direct WS client authentication handshake (first message MUST be auth).

Moved verbatim from ws_client_handler.py on 2026-09-07 (lines 318-487):
protocol-version negotiation, API-key check via AuthGateway, per-profile
connection-limit enforcement and rate-limiter bootstrap.
"""

import asyncio
import json

from config import WS_HANDSHAKE_TIMEOUT, PROTOCOL_VERSION

from ..connection_enforcement import conn_tracker, rate_limiter
from .log import logger


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

async def _authenticate_direct_client(ws, server_provider, peer):
    """Authenticate a direct WS client.

    Returns:
        Tuple of (user_info_dict, api_key_string, conn_id) on success, or
        (None, None, None) when the client is rejected/disconnects during auth.
        (2026-09-01 B2 doc fix: the old one-line summary described a 2-tuple; the
        connection-tracking conn_id has been part of the return since Fix #2.)
    
    CONNECTION ENFORCEMENT (Fix #2):
        - After successful auth, checks max_connections limit per API key.
        - Registers the connection in conn_tracker for cleanup on disconnect.
        - Returns tuple of (user_info, api_key) so caller can track enforcement state.
    
    ROLE PROPAGATION (Fix #3 - 2026-05-28, superseded):
        - Stores user_info on ws._user_info for downstream access in chat_relay.py.
        - The old set_current_user_role()/set_python_exec_blocked_libs() calls were
          removed 2026-08-28: this SERVER never executes tools, so those stores had no
          live consumer. Profile data (role + blocked-libs) reaches tool code via the
          tools_response payload instead.
    """
    import websockets.exceptions as ws_exc

    try:
        raw_auth_msg = await asyncio.wait_for(ws.recv(), timeout=WS_HANDSHAKE_TIMEOUT)
        auth_msg = json.loads(raw_auth_msg)

        if auth_msg.get("type") != "auth":
            logger.warning(f"[SERVER] Direct WS client {peer} sent non-auth first message -- rejecting")
            await ws.send(json.dumps({"type": "error", "message": "Authentication required as first message"}))
            await ws.close()
            return None, None, None

        # PROTOCOL VERSION NEGOTIATION (2026-08-20): the client declares its protocol
        # version in the auth frame. A missing field means a legacy v1 client -- tolerated
        # for one release cycle with a loud warning; an explicit mismatch is rejected so
        # the two ends never silently speak different dialects of the JSON protocol.
        client_proto = auth_msg.get("protocol_version")
        if client_proto is None:
            logger.warning(f"[SERVER] Direct WS client {peer} sent no protocol_version (legacy v1) -- "
                           f"server runs PROTOCOL_VERSION={PROTOCOL_VERSION}. Upgrade the CLIENT.")
        else:
            try:
                client_proto = int(client_proto)
            except (TypeError, ValueError):
                logger.warning(f"[SERVER] Direct WS client {peer} sent invalid protocol_version={client_proto!r}")
                await ws.send(json.dumps({"type": "error", "message": f"Invalid protocol_version: {client_proto!r}"}))
                await ws.close()
                return None, None, None
            if client_proto != PROTOCOL_VERSION:
                logger.warning(f"[SERVER] Direct WS client {peer} protocol mismatch: client={client_proto}, server={PROTOCOL_VERSION}")
                await ws.send(json.dumps({
                    "type": "error",
                    "message": (f"Protocol version mismatch: CLIENT speaks v{client_proto}, SERVER speaks v{PROTOCOL_VERSION}. "
                                f"Update the {'CLIENT' if client_proto < PROTOCOL_VERSION else 'SERVER'} so both ends agree.")
                }))
                await ws.close()
                return None, None, None

    
        client_key = auth_msg.get("api_key", "")
        if not client_key:
            logger.warning(f"[SERVER] Direct WS client {peer} sent empty API key -- rejecting")
            await ws.send(json.dumps({"type": "error", "message": "Empty API key"}))
            await ws.close()
            return None, None, None

        # Smart authentication via AuthGateway (profile-based permissions)
        user_info = server_provider._auth_gateway.authenticate(client_key)
        if not user_info:
            logger.warning(f"[SERVER] Direct WS client {peer} auth failed -- rejecting")
            await ws.send(json.dumps({"type": "error", "message": "Authentication failed or no valid profile"}))
            await ws.close()
            return None, None, None

        # --- Connection limit enforcement (Fix #2) ---
        max_connections = user_info.get("max_connections", 1)
        if not await conn_tracker.can_connect(client_key, max_connections):
            current_count = await conn_tracker.get_active_count(client_key)
            logger.warning(
                f"[SERVER] Direct WS client {peer} connection REJECTED — "
                f"key={client_key[:8]}... already has {current_count}/{max_connections} connections"
            )
            await ws.send(json.dumps({
                "type": "error",
                "message": (
                    f"Connection limit reached ({current_count}/{max_connections}). "
                    f"Close existing connections or upgrade your profile."
                )
            }))
            await ws.close()
            return None, None, None

        # Register this connection in the tracker
        conn_id = await conn_tracker.register(client_key, ws)
        if not conn_id:
            logger.error(f"[SERVER] Failed to register connection for {peer}")
            await ws.send(json.dumps({"type": "error", "message": "Internal server error"}))
            await ws.close()
            return None, None, None

        # Start rate limiter cleanup task (lazy start)
        await rate_limiter.start_cleanup()

        # NOTE (2026-08-28): the former auth-time set_current_user_role() /
        # set_python_exec_blocked_libs() calls were removed. This SERVER never executes
        # tools, so those tools.utils stores had no live consumer here -- the profile
        # role and blocked-libs set reach tool code via the tools_response payload
        # (config_constants['CURRENT_USER_ROLE'] + python_exec_blocked_libs), where the
        # CLIENT injects them into tool globals at compile time.

        # Store user_info on ws object for downstream access in chat_relay.py etc.
        ws._user_info = user_info

        # Send auth success with role + capabilities to client
        local = server_provider._get_local_provider()
        # (2026-08-23) get_models_list() entries carry the "folder" identity used by
        # exact-match authorization. API id strings have no folder field and would be
        # denied fail-closed for non-admins, so they must not feed this filter.
        all_model_entries = await local.get_models_list()

        allowed_models = server_provider._auth_gateway.get_allowed_models(
            user_info, all_model_entries
        )
        allowed_tools = user_info.get("allowed_tools")  # None=all or list

        # Get actual context window from server config
        from config import CONTEXT_WINDOW_TOKENS as SERVER_CONTEXT_WINDOW

        await ws.send(json.dumps({
            "type": "auth_ok",
            "protocol_version": PROTOCOL_VERSION,   # Echoed so the CLIENT can verify both ends agree
            "role": user_info["role"],
            "email": user_info["email"],
            "allowed_models": [m if isinstance(m, str) else m.get("name", "") for m in allowed_models],
            "allowed_tools": allowed_tools,  # None = all tools, list = restricted
            "context_window": SERVER_CONTEXT_WINDOW  # Actual model context window from server config
        }))
        ws._conn_id = conn_id  # Store for cleanup on disconnect (Fix #2)
        logger.info(f"[SERVER] Direct WS client authenticated: role={user_info['role']}, conn_id={conn_id}")
        
        # Return (user_info, api_key) — caller stores these for enforcement
        return user_info, client_key, conn_id

    except asyncio.TimeoutError:
        logger.warning(f"[SERVER] Direct WS client {peer} auth timeout -- disconnecting")
        try: await ws.close()
        except Exception: pass
        return None, None, None
    except json.JSONDecodeError as e:
        logger.error(f"[SERVER] Invalid JSON in auth from direct WS client {peer}: {e}")
        try: await ws.close()
        except Exception: pass
        return None, None, None
    # Expected connection drops during model switch - log at INFO, not ERROR
    except (ws_exc.ConnectionClosed, ws_exc.ConnectionClosedError) as e:
        logger.info(f"[SERVER] Direct WS client {peer} disconnected during auth (expected during model reload)")
        try: await ws.close()
        except Exception: pass
        return None, None, None
    except ConnectionAbortedError as e:
        logger.info(f"[SERVER] Direct WS client {peer} connection aborted during auth (expected)")
        try: await ws.close()
        except Exception: pass
        return None, None, None
    except Exception as e:
        logger.error(f"[SERVER] Auth error for direct WS client {peer}: {e}", exc_info=True)
        try: await ws.close()
        except Exception: pass
        return None, None, None
