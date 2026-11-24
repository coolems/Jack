"""WebSocket chat handler - the main orchestrator.

Coordinates:
- Connection setup and cleanup
- API key authentication with max-connections enforcement
- Message parsing and validation
- /search command dispatch
- File/media processing
- Tool detection (informational only - agentic mode is enabled ONLY via the UI button)
- Live steering: steer=True frames go to the live agentic turn instead of submit()
- Submitting validated turns to the ChatBus (the turn runs as a background task owned by
  the chat's channel; partial persistence on UI disconnect is the channel's capture)
- Persistence: user messages here; assistant messages and error/retry handling in ChatBus
- Role-based tool filtering
"""

import asyncio
import json
import logging
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect

from app.helpers import get_ws_client_ip
from app.prompts import get_chat_prompt, get_system_prompt
from app.schemas import WebSocketChatMessage
from app.workspace_tree import is_workspace_id
from client_config import load_search_engines

# Generation frames are emitted by the ChatBus while the turn runs.
from app.websocket.message_types import send_error
# Assistant-message persistence is owned by the ChatBus; user messages are still saved here
# in stage 2 before the turn is submitted.
from app.websocket.db_ops import save_user_message

from app.websocket.file_handler import process_media_files
from app.websocket.search_handler import handle_search_command

from app.detection import detect_tool_requirement

from utils.history_manager import (
    load_conversation_history as load_history_from_db,
    count_messages_for_conv,
    trim_history_to_token_limit,
    max_history_tokens,
    )

logger = logging.getLogger("COOLEMS")
ws_logger = logging.getLogger("COOLEMS.WebSocket")


def _conversation_model(conv_id: str) -> str | None:
    """Return the saved model, or None for a workspace with no assignment."""
    from app.db_manager import get_db_connection, close_db_connection

    if not conv_id:
        return None
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT model FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return (row[0] or None) if row else None
    finally:
        close_db_connection(conn)


def _is_user_cancelled(error) -> bool:
    """Check if an error/result is a user-initiated cancellation."""
    if not isinstance(error, BaseException):
        return False
    error_class_name = type(error).__name__
    if "cancel" in error_class_name.lower():
        return True
    # WebSocketDisconnect from FastAPI when client disconnects = user stopped
    if error_class_name == "WebSocketDisconnect":
        return True
    # ConnectionClosed variants (from websockets library) also count as cancellation
    if error_class_name in ("ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError"):
        return True
    return False


def create_websocket_handler(
    db_path: str,
    provider,
    model_name: str,
    api_timeout: int,
    agent,
    tool_orchestrator,
    stop_events: dict,
    bus=None,
    api_keys: Set[str] | None = None,
):
    """Factory function that creates the WebSocket chat handler.

    Args:
        db_path: Path to the SQLite database.
        provider: The AI provider instance (OllamaProvider or LlamaProvider).
        model_name: Default model name.
        api_timeout: API request timeout in seconds.
        agent: The agent DNA instance.
        tool_orchestrator: Tool orchestrator instance.
        stop_events: Dict mapping conv_id -> asyncio.Event for stop control.
        api_keys: Set of valid API keys for authentication.

    Returns:
        The websocket_chat async function.
    """
    # Imported at FACTORY level (not inside a nested function) because the finally-block of
    # websocket_chat below calls release_websocket_session -- a name bound only inside
    # _authenticate_and_setup's local scope would be invisible there and raise NameError on
    # every disconnect. app.auth has no imports back into this module, so no circular import.
    from app.auth import authenticate_websocket, release_websocket_session

    # (2026-11-15 consent answer channel) the shared delivery path of ExecApprovalState,
    # used by the 'exec_approval_decision' frame handled in websocket_chat below.
    from app.chat_bus.exec_approval import handle_decision_frame

    # (2026-08-23 fix, DEFECT A): broadcast-registry imports at FACTORY level so both
    # _authenticate_and_setup (register) and websocket_chat's finally-block (unregister)
    # can see them — a name bound only inside one nested function would raise NameError
    # in the other. connections.py imports nothing from this module, so no circular import.
    from app.websocket.connections import register_connection, unregister_connection

    async def _authenticate_and_setup(websocket, conv_id):
        """Stage 1: authenticate + per-connection setup.

        Returns a context dict on success; None if the client was rejected or closed
        during auth (the connection is already handled by authenticate_websocket).
        """
        # authenticate_websocket / release_websocket_session are imported at factory level
        # (see create_websocket_handler) so the finally-block of websocket_chat can also see them.
        email_param = websocket.query_params.get("email", "").strip()
        authenticated, ws_key, ws_session_id = await authenticate_websocket(websocket, api_keys, email=email_param)
        if not authenticated:
            # Connection already closed by authenticate_websocket
            return None

        if not is_workspace_id(conv_id):
            try:
                await websocket.accept()
                await send_error(
                    websocket, "This workspace ID is out of date. Reload the page and reopen the workspace.", code=409
                )
                await websocket.close(code=1008)
            finally:
                if ws_key:
                    release_websocket_session(ws_key, session_id=ws_session_id)
            return None

        await websocket.accept()

        client_ip = get_ws_client_ip(websocket)

        register_connection(websocket, client_ip=client_ip, conv_id=conv_id)

        # Get role for logging
        from app.keys import get_key_role
        role = get_key_role(ws_key) if ws_key else "unknown"

        logger.debug(f"WebSocket connected for conversation at {client_ip}")

        stop_event = asyncio.Event()
        # (2026-09-08 multi-chat) this connection SUBSCRIBES to the chat's channel:
        # generation runs as a background task owned by the ChatBus, so switching chats
        # in the browser never kills another chat's session. The channel owns the stop
        # event (the legacy stop_events dict is kept in sync by the bus for /api/stop).
        if bus is not None:
            bus.set_ws_key(ws_key)
            _channel = bus.attach(websocket, conv_id)
            # replay frames that were buffered while this chat had no UI socket attached
            for _fr in _channel.buffered_frames():
                try:
                    await websocket.send_text(_fr)
                except Exception:
                    pass  # dead socket — the channel keeps buffering

            # (2026-09-08 multi-chat UI fix): mark where the buffered tail ends so the UI can
            # drop its replay guard and treat every later frame as LIVE. Without this marker a
            # re-attaching client could not tell replayed chunks apart from fresh ones.
            try:
                await websocket.send_text(json.dumps({"type": "replay_end"}))
            except Exception:
                pass  # dead socket — the channel keeps buffering

            # (2026-09-08 multi-chat UI fix): AUTHORITATIVE STATE HAND-OFF. After the buffered
            # tail is replayed, tell the re-attaching UI exactly where this chat's session stands
            # right now. That is what restores the generation indicator and the send/stop button
            # color when switching back to a chat (the reported bug: the switch lost that state).
            try:
                await websocket.send_text(json.dumps({
                    "type": "chat_status",
                    "status": _channel.status.value,
                    "queue_position": int(getattr(_channel, "queue_position", 0) or 0),
                    # (2026-10-23 live steering) lets a re-attaching UI know whether the
                    # running turn can be steered (agentic) or must be stopped normally.
                    "agent_mode": bool(getattr(_channel, "agent_mode", False)),
                    }))
            except Exception:
                pass  # dead socket — the channel keeps buffering

        stop_event = _channel.stop_event if bus is not None else asyncio.Event()

        return {
            "websocket": websocket,   # per-connection refs carried into later stages
            "conv_id": conv_id,
            "ws_session_id": ws_session_id,  # needed by the finally-block (release_websocket_session)
            "ws_key": ws_key,
            "role": role,
            "client_ip": client_ip,
            "stop_event": stop_event,  # channel-owned (bus) or local fallback
            # per-connection mutable state shared with the later stages:
            "response_in_progress": False,
            "last_message_count": 0,
            "history": [],
        }


    async def _prepare_user_message(payload, ctx):
        """Stage 2: turn one validated payload into a ready-to-dispatch dict.

        Handles: field extraction, stop/capture reset, /search short-circuit, media file
        processing, tool detection + auto agent-mode, user-message persistence and history
        load/trim, role-based tool filtering. Returns the prepared dict, or None when the
        message was fully handled by /search (caller should just `continue`).

        Mutates ctx in place for shared per-connection state: response_in_progress, history.
        """
        ws = ctx["websocket"]
        cid = ctx["conv_id"]
        ws_key, role, client_ip = ctx["ws_key"], ctx["role"], ctx["client_ip"]
        stop_event = ctx["stop_event"]  # channel-owned (bus) or local fallback

        # Step 3: Extract validated fields (all type-safe now)
        user_msg = payload.message
        # (2026-12-04 per-workspace model) precedence: explicit per-request model >
        # the model stored on THIS workspace's row > the global default.
        model = payload.model or _conversation_model(cid) or model_name
        # The SERVER delivers the finished prompt (identity + tool contract); a
        # per-request override from the client wins if one is actually sent.
        system_prompt = payload.system_prompt or get_system_prompt()
        agent_mode = payload.agent_mode
        media_files = payload.media_files
        # Engine config lives in <CLIENT>/client_config/search_engines.json (single source of
        # truth). Read it fresh on every message so Settings UI saves apply immediately -
        # no restart needed. (payload.search_engines is a legacy field, ignored here.)
        search_engines = {"engines": load_search_engines()}
        enable_thinking = payload.enable_thinking

        logger.info(f"[AGENT] Client {client_ip} (role={role}) sent: {user_msg[:100]}...")

        stop_event.clear()
        ctx["response_in_progress"] = True

        # --- Handle /search command ---
        if user_msg and user_msg.strip().lower().startswith("/search "):
            search_query = user_msg.strip()[8:].strip()
            logger.info(
                f"[SEARCH] Web search triggered via /search command: {search_query}"
            )

            success = await handle_search_command(
                user_msg=user_msg,
                search_query=search_query,
                model=model,
                provider=provider,
                tool_orchestrator=tool_orchestrator,
                websocket=ws,
                agent=agent,
                db_path=db_path,
                conv_id=cid,
                search_engines=search_engines,
                conversation_history=ctx["history"],
                api_key=ws_key,  # Pass API key for permission checking
            )

            if success:
                ctx["response_in_progress"] = False
                return None

        # --- Handle file uploads ---
        enhanced_message, image_data, image_paths, file_contents = (
            # Per-workspace working root (2026-10-09): resolve uploads against THIS chat's
            # own folder so concurrent workspaces never mix up upload locations.
            process_media_files(user_msg, media_files, conv_id=cid)
        )

        # --- Tool detection (informational only) ---
        # Agentic mode is enabled ONLY when the user presses the agent button at the
        # prompt input window (payload.agent_mode). Detection results are kept solely to
        # drive the "search on internet" suggestion below - they must NEVER flip
        # agent_mode on their own.
        requires_tools, _tool_types = detect_tool_requirement(user_msg)
        # --- Save user message ---
        changed_titles = []
        save_user_message(
            db_path, cid, user_msg,
            media_urls=media_files,
            file_contents=file_contents,
            on_title_changed=changed_titles.append,
        )
        if changed_titles:
            frame = {"type": "workspace_title_changed"}
            channel = bus.channels.get(cid) if bus is not None else None
            if channel is not None:
                await channel.emit_frame(frame)
            else:
                try:
                    await ws.send_text(json.dumps(frame))
                except (RuntimeError, OSError, WebSocketDisconnect):
                    logger.debug("Could not deliver workspace title notification")

        # --- Reload and trim history ---
        ctx["history"] = load_history_from_db(cid)
        ctx["history"] = trim_history_to_token_limit(
            ctx["history"],
            max_tokens=max_history_tokens(),
        )

        # ============================================================
        # ROLE-BASED TOOL FILTERING
        # Get tools filtered by the user's role
        # ============================================================
        ollama_tools = None
        if agent_mode and tool_orchestrator:
            # Intent-gated starter set (2026-11-20): send only the tool folders matching
            # this message's detected intent + the request_tools escape hatch. The model
            # discovers the rest via the catalog and pulls it on demand, so a plain "hi"
            # no longer pays for ~63 full tool schemas. starter_tools is async — the lazy
            # SERVER fetch runs directly on this loop.
            ollama_tools = await tool_orchestrator.starter_tools(
                api_key=ws_key, tool_types=_tool_types,
            )

            # Log the gated starter set actually sent to this role.
            if ollama_tools:
                tool_names = [t.get("function", {}).get("name", "unknown") for t in ollama_tools]
                logger.info(f"[ROLE] Role '{role}' starter set ({len(ollama_tools)} tools): {tool_names}")
            else:
                logger.info(f"[ROLE] Role '{role}' has NO tools available (agent mode may not work)")

        # --- Prompt for this turn (SERVER-delivered text, nothing composed here) ---
        # Tools available -> the full delivered prompt (identity + tool contract).
        # No tools -> the delivered plain-chat prompt (identity only), so a normal
        # chat never pays for the tool contract. A per-request override wins.
        turn_prompt = system_prompt or (get_system_prompt() if ollama_tools else get_chat_prompt())

        return {
            "user_msg": user_msg,
            "model": model,
            "system_prompt": turn_prompt,
            "agent_mode": agent_mode,
            "enable_thinking": enable_thinking,
            "enhanced_message": enhanced_message,
            "image_data": image_data,
            "image_paths": image_paths,
            "file_contents": file_contents,
            "media_files": media_files,
            "requires_tools": requires_tools,
            "ollama_tools": ollama_tools,
        }


    async def websocket_chat(websocket: WebSocket, conv_id: str):
        # (2026-08-20 split, review item #10): three stages -- _authenticate_and_setup,
        # per-message parsing/validation in the loop, and _prepare_user_message. Behavior
        # is unchanged; each stage now has one job and can be read/tested on its own.

        ctx = await _authenticate_and_setup(websocket, conv_id)
        if ctx is None:
            return  # rejected/closed during auth

        ws_key, client_ip = ctx["ws_key"], ctx["client_ip"]
        ws_session_id = ctx["ws_session_id"]  # used by the finally-block below

        try:
            while True:
                # sync per-connection state from the shared context each iteration
                last_message_count = ctx["last_message_count"]

                current_count = count_messages_for_conv(conv_id)

                if current_count != last_message_count:
                    logger.info(
                        f"History changed: was {last_message_count}, "
                        f"now {current_count} messages"
                    )
                    last_message_count = current_count
                    ctx["last_message_count"] = current_count

                # --- VALIDATED INPUT PARSING ---
                data = await websocket.receive_text()

                # Step 1: Parse JSON
                try:
                    raw_payload = json.loads(data)
                except json.JSONDecodeError as e:
                    logger.warning(f"[WS] Invalid JSON from client {client_ip}: {e}")
                    await send_error(websocket, f"Invalid JSON: {str(e)}", code=400)
                    continue

                # ============================================================
                # (2026-11-15 consent answer channel) A consent-card decision is NOT a chat
                # message: it carries no text, so it is routed BEFORE the WebSocketChatMessage
                # validation (which would reject it with 422). The UI sends it on the SAME
                # socket that delivered the card: the old one-shot HTTP POST died whenever the
                # page was torn down or the workspace was switched ("Failed to fetch"), the
                # click was lost and the turn stayed parked on the pending future forever.
                # conv_id comes from THIS socket, never from the payload.
                # ============================================================
                if isinstance(raw_payload, dict) and raw_payload.get("type") == "exec_approval_decision":
                    ack = handle_decision_frame(bus, conv_id, raw_payload)
                    try:
                        await websocket.send_text(json.dumps(ack))
                    except Exception:
                        logger.debug("[EXEC-APPROVAL] ws ack could not be delivered (UI socket gone)")
                    continue

                # Step 2: Validate with Pydantic
                try:
                    payload = WebSocketChatMessage.model_validate(raw_payload)
                except Exception as e:
                    error_detail = str(e)
                    logger.warning(
                        f"[WS] Validation error from client {client_ip}: {error_detail}"
                    )
                    await send_error(websocket, f"Invalid message: {error_detail}", code=422)
                    continue

                # ============================================================
                # (2026-10-23 live steering) A steer is NOT a new turn: it must never
                # save a user message, reset per-connection state or supersede the
                # running task (bus.submit does exactly that). Route it to the live
                # channel instead; the ReAct loop picks it up at its next safe step.
                # ============================================================
                if getattr(payload, "steer", False):
                    if bus is None:
                        await send_error(websocket, "Chat session service unavailable (internal error). Please retry.", code=500)
                        continue
                    ok, info, code = await bus.steer(conv_id, payload.message, payload.steer_id)
                    # turn_id + at_ts ride on BOTH ack frames: the UI paints the steer as a
                    # ROW of the turn's loop-history block (never a chat bubble), placed at
                    # the moment the user typed it and settled by these acks.
                    _ch = bus.channels.get(conv_id)
                    _turn_id = str(getattr(_ch, "turn_id", "") or "") if _ch is not None else ""
                    _at_ts = info.get("received_at") if isinstance(info, dict) else None
                    if ok:
                        frame = {
                            "type": "steer_received",
                            "steer_id": info.get("id"),
                            "turn_id": _turn_id,
                            "content": payload.message,
                            "at_ts": _at_ts,
                            "pending": _ch.pending_steer_count() if _ch is not None else 1,
                        }
                        logger.info(f"[STEER] conv={conv_id} steering message received "
                                    f"(id={str(info.get('id'))[:12]})")
                        # The row of the turn's loop-history block, at the moment the user
                        # typed it. Sent through the CHANNEL (not this socket only), so a
                        # workspace that is in the background gets the same entry.
                        if _ch is not None:
                            try:
                                await _ch.emit_frame({
                                    "type": "cycle_steer",
                                    "conv_id": conv_id,
                                    "turn_id": _turn_id,
                                    "steer_id": info.get("id"),
                                    "text": payload.message,
                                    "applied": False,
                                    "received": True,
                                    "step_no": 0,
                                    "at_ts": _at_ts,
                                })
                            except Exception:
                                logger.debug("[STEER] cycle_steer row frame not delivered")
                    else:
                        frame = {
                            "type": "steer_rejected",
                            "steer_id": payload.steer_id,
                            "turn_id": _turn_id,
                            "content": payload.message,
                            "at_ts": _at_ts,
                            "code": code,
                            "message": info,
                        }
                        logger.info(f"[STEER] conv={conv_id} steering message refused ({code}): {info}")
                    try:
                        await websocket.send_text(json.dumps(frame))
                    except Exception:
                        logger.debug("[STEER] ack could not be delivered (UI socket gone)")
                    continue

                prepared = await _prepare_user_message(payload, ctx)
                if prepared is None:
                    # /search handled the message end-to-end -- next frame.
                    continue

                # ============================================================
                # (2026-09-08 multi-chat) SUBMIT TO THE CHAT BUS — the turn now runs
                # as a background task owned by this chat's channel. The receive loop
                # returns immediately, so this UI socket can keep serving frames and
                # (crucially) closing it no longer kills another chat's generation.
                # Frames stream back through the channel: live while attached,
                # buffered otherwise. Final answer is persisted by the bus itself.
                # ============================================================
                if bus is None:
                    await send_error(websocket, "Chat session service unavailable (internal error). Please retry.", code=500)
                    continue

                prepared["history"] = ctx["history"]
                await bus.submit(websocket, conv_id, prepared)

        except WebSocketDisconnect:
            logger.debug(f"WebSocket closed at {client_ip}")
            # (2026-09-08 multi-chat) closing this UI socket NO LONGER kills the chat's
            # generation: the turn runs in the ChatBus and keeps streaming into its channel
            # (live push or bounded background buffer). We only detach this subscriber.
            if bus is not None:
                bus.detach(websocket, conv_id)
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            try:
                await send_error(websocket, str(e))
            except Exception:
                logger.debug("Send operation failed silently at CLIENT/app/websocket/handler.py")

        finally:
            # DISCARD TOOLS AFTER USE (2026-09-08 multi-chat): the shared tool cache is
            # cleared only when NO chat session is live anymore. Clearing it on every UI
            # socket close would wipe tools out from under OTHER chats still generating.
            if bus is not None and tool_orchestrator is not None:
                _live = any(ch.task is not None and not ch.task.done() for ch in bus.channels.values())
                if not _live:
                    try:
                        tool_orchestrator.clear_tools()
                    except Exception as e:
                        logger.warning(f"Failed to clear tools on quiet-down: {e}")

            # Release the session slot so the key can be used again
            # (2026-08-23 fix, DEFECT A): drop this connection from the broadcast registry.
            try:
                unregister_connection(websocket, client_ip=client_ip, conv_id=conv_id)
            except Exception as e:
                logger.debug(f"Unregister connection failed (harmless): {e}")

            if ws_key:
                release_websocket_session(ws_key, session_id=ws_session_id)

            # (2026-09-08 multi-chat) stop-event cleanup moved to the ChatBus:
            # generation_task._guarded_turn() unregisters its channel's event when the
            # turn ends, so this connection's finally must NOT touch stop_events anymore.
    return websocket_chat