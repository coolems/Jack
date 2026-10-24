"""Authenticated message loop for one direct WS client (per-frame router).

Moved verbatim from ws_client_handler.py on 2026-09-07 (lines 178-312): routes
every frame by type -- request/stop with owner-scoped stop events + rate
limiting, health_check/models_request/current_model/model_switch, and the
tools/tool-code/OCR handlers. Disconnect cleanup stays in ws_entry's finally.
"""

import json

from config import LLAMA_SERVER_TEMP, PROFILE_DEFAULT_RATE_LIMIT

from app.auth_gateway.model_resolver import is_model_allowed

from ..connection_enforcement import rate_limiter
from .active_conversations import _active_convs
from .log import logger
from .model_switch_handler import _handle_model_switch_direct
from .ocr_handler import _handle_ocr_request
from .tool_code_handler import _handle_tool_code_request
from .tools_request_handler import _handle_tools_request

async def _run_authenticated_loop(ws, server_provider, user_info, conn_id, authenticated_api_key, client_convs):
    """Authenticated message loop for one direct WS client (extracted 2026-08-20).

    Routes every frame by type: request/stop with owner-scoped stop events + rate limiting,
    health_check/models_request/current_model/model_switch with req_id echo (persistent
    control channel), tools_request/tool_code_request. All exceptions are handled here;
    the caller's finally-block performs disconnect cleanup using *client_convs* (mutated in place).

    FIXED (2026-08-21): body dedented to module-function level -- a splice left it 4 spaces too deep,
    which is an IndentationError at import time.
    """
    import websockets

    peer = ws.remote_address if hasattr(ws, 'remote_address') else "unknown"

    try:
        async for raw_msg in ws:
            if not server_provider._running:
                break

            msg = json.loads(raw_msg)
            t = msg.get("type", "")
            logger.info(f"[SERVER<-CLIENT] Direct WS frame from {peer}: type={t}")

            # --- Rate limiting enforcement for chat requests (Fix #2) ---
            if t == "request":
                max_rate = user_info.get("max_rate_limit", PROFILE_DEFAULT_RATE_LIMIT)
                if not await rate_limiter.allow_request(authenticated_api_key, max_rate):
                    logger.warning(
                        f"[SERVER] Rate limit exceeded for key={authenticated_api_key[:8]}... "
                        f"(limit={max_rate}/s) from {peer}"
                    )
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": f"Rate limit exceeded ({max_rate} requests/sec). Slow down."
                    }))
                    continue  # Skip processing this request

                # --- Per-request model authorization (H1 fix, 2026-09-08; fleet 2026-10) ---
                # Mirrors the relay path. profiles.json lists the model FILENAMES this
                # role may use ("allowed_models"); the resolver is exact, case-insensitive
                # basename matching - no folders, no substrings, no fuzzy logic.
                _req_model = msg.get("model", "") or ""
                if _req_model:  # empty model is rejected by the queue (no model specified)
                    if not is_model_allowed(user_info, _req_model):
                        logger.warning(
                            f"[SERVER] Chat request DENIED via direct WS: model '{_req_model}' "
                            f"not allowed for role={user_info.get('role')} from {peer}"
                        )
                        await ws.send(json.dumps({
                            "type": "error",
                            "message": f"Model '{_req_model}' is not available for your profile.",
                        }))
                        continue

                conv_id = msg.get("conv_id", "")
                model = msg.get("model", "?")
                n_msgs = len(msg.get("messages", []))

                # using the same conv_id cannot clobber each other's stop events.
                # using the same conv_id cannot clobber every other connection's events.
                # 2026-09-08 multi-chat: ALWAYS registered (even with empty conv_id) —
                # the queued request needs its stop event to be cancellable at any time.
                server_stop_event = _active_convs.register(conn_id, conv_id)
                client_convs.add(_active_convs._key(conn_id, conv_id))

                logger.info(f"[SERVER<-CLIENT] Chat request via Direct WS: model={model}, msgs={n_msgs}, conv={conv_id}")

                # (2026-09-08 multi-chat) ENQUEUE instead of inline await: the receive loop
                # returns immediately, so this connection can carry further frames (stop,
                # health checks, or another chat's request) while generation runs in the
                # worker pool. Frames come back over THIS socket via the request's sink.
                from app.server_queue import ChatRequest, DirectWsSink, get_queue
                from app.providers.llama.backend_registry import get_registry

                queue = get_queue()
                if queue is None:
                    logger.error("[SERVER] Request queue not initialized - cannot accept chat requests")
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": "Server request queue unavailable (internal error). Please retry.",
                    }))
                    continue

                from ..chat_relay import _filter_tools_by_profile
                req = ChatRequest(
                    owner_key=f"{conn_id}|{conv_id}",
                    user_key=user_info.get("email") or str(conn_id),
                    max_parallel=int(user_info.get("max_parallel_slots") or 0),
                    sink=DirectWsSink(ws),
                    model=msg.get("model", ""),
                    messages=msg.get("messages", []),
                    temperature=msg.get("temperature", LLAMA_SERVER_TEMP),
                    enable_thinking=bool(msg.get("enable_thinking", False)),
                    conv_id=conv_id,
                    tools=_filter_tools_by_profile(server_provider, user_info,
                                                   msg.get("tools") if msg.get("tools") else None),
                    stop_event=server_stop_event,
                )

                # (2026-10 fleet) "bad luck, dude" path: no node serves this model at all.
                # Configured-but-down models are queued and handled by the worker watchdog.
                reg = get_registry()
                if reg is not None and not reg.model_configured(req.model_key):
                    logger.warning(
                        "[SERVER] Chat request for model=%s (key=%s) has no configured node. "
                        "Fleet: %s", req.model, req.model_key or "?", reg.describe_models(),
                    )
                    # Server-side unavailability, not the user's fault: refund the token.
                    await rate_limiter.refund(
                        authenticated_api_key,
                        user_info.get("max_rate_limit", PROFILE_DEFAULT_RATE_LIMIT),
                    )
                    from ..model_catalog import available_models_hint
                    hint = await available_models_hint(server_provider, user_info)
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": (f"No llama node is serving '{req.model}' right now. "
                                    f"Available: {hint}."),
                        "retryable": False,
                        "model": req.model_key,
                    }))
                    continue

                res = await queue.enqueue(req)
                if not res.accepted:
                    logger.warning("[SERVER] Chat request rejected for conv=%s: %s",
                                   conv_id, res.reason)
                    if res.retryable:
                        # Server-side rejection: do not charge the rate limiter for it.
                        await rate_limiter.refund(
                            authenticated_api_key,
                            user_info.get("max_rate_limit", PROFILE_DEFAULT_RATE_LIMIT),
                        )
                    await ws.send(json.dumps({
                        "type": "error",
                        "message": res.reason,
                        "retryable": bool(res.retryable),
                        "retry_after_sec": res.retry_after_sec,
                        "model": res.model_key,
                    }))
                    continue

                # Let the chat know it is waiting behind other requests (per model group).
                if res.position > 1:
                    await ws.send(json.dumps({
                        "type": "queue_status",
                        "conv_id": conv_id,
                        "position": res.position,
                        "model": res.model_key,
                    }))

            elif t == "stop":
                # Client explicitly requested stop for a conversation.
                # FIX (2026-08-19): owner-scoped -- the composite key includes this
                # connection's conn_id, so a client can only stop conversations IT owns
                # (previously any authenticated client could stop any conv_id globally).
                conv_id = msg.get("conv_id", "")
                if conv_id:
                    stopped_it = _active_convs.unregister_and_stop(conn_id, conv_id)
                    logger.info(f"[SERVER] Stop request received for conv={conv_id} "
                                f"(owned by this connection: {stopped_it})")

            elif t == "health_check":
                logger.info(f"[SERVER<-CLIENT] Health check via Direct WS from {peer}")
                local = server_provider._get_local_provider()
                ok, err = (await local.health_check(force=False))[:2]
                # (2026-10 fleet) The SERVER is healthy when ANY node serves traffic -
                # the brain's local node being down must not report the whole fleet down.
                from app.providers.llama.backend_registry import get_registry
                _reg = get_registry()
                if _reg is not None and _reg.total_slots() > 0:
                    ok, err = True, ""
                # catalog = local disk models + every model the fleet serves, filtered
                # by the caller's profile (filenames).
                from ..model_catalog import build_model_catalog
                models = await build_model_catalog(server_provider, user_info if user_info else None)
                await ws.send(json.dumps({
                    "type": "health_ok" if ok else "error",

                    # req_id echoed for the CLIENT persistent control channel (2026-08-20)

                    "req_id": msg.get("req_id"),
                    "models": models,
                    "message": err
                }))

            elif t == "models_request":
                logger.info(f"[SERVER<-CLIENT] Models request via Direct WS from {peer}")
                from ..model_catalog import build_model_catalog
                models = await build_model_catalog(server_provider, user_info if user_info else None)
                await ws.send(json.dumps({
                    "type": "models_response",

                    "req_id": msg.get("req_id"),
                    "models": models
                }))

            elif t == "current_model":
                # (2026-08-23 fix) force=True bypasses the fresh model-state cache so a
                # post-switch verification reports the NEW model immediately.
                logger.info(f"[SERVER<-CLIENT] Current model request via Direct WS from {peer}")
                local = server_provider._get_local_provider()
                current = (await local.get_current_model(force=bool(msg.get("force")))) or ""
                await ws.send(json.dumps({
                    "type": "current_model_response",

                    "req_id": msg.get("req_id"),
                    "model": current
                }))

            elif t == "model_switch":
                model_filename = msg.get("model", "")
                logger.info(f"[SERVER<-CLIENT] Model switch request via Direct WS from {peer}: {model_filename}")
                await _handle_model_switch_direct(ws, server_provider, user_info, model_filename, req_id=msg.get("req_id"))

            elif t == "tools_request":
                logger.info(f"[SERVER<-CLIENT] Tools request via Direct WS from {peer}")
                await _handle_tools_request(ws, user_info, server_provider)

            elif t == "tool_code_request":
                logger.info(f"[SERVER<-CLIENT] Tool code request via Direct WS client {peer}: name={msg.get('name', '?')}")
                await _handle_tool_code_request(ws, msg, user_info, server_provider)

            elif t == "ocr_request":
                # (2026-08-23 revive) OCR runs HERE on the SERVER: the local LlamaProvider
                # owns the exact GLM-OCR call format ("Text Recognition:" prompt). The CLIENT
                # ships pixels + model names; transcribed text comes back in ocr_response.
                logger.info(f"[SERVER<-CLIENT] OCR request via Direct WS from {peer}: models={msg.get('ocr_models')}")
                await _handle_ocr_request(ws, msg, server_provider)

    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"[SERVER] Direct WS client {peer} connection closed: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"[SERVER] Invalid JSON from direct WS client {peer}: {e}")
    except Exception as e:
        logger.error(f"[SERVER] Error handling direct WS client {peer}: {e}", exc_info=True)
