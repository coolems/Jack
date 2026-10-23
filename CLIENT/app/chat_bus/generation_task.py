"""Background generation turn runner (2026-09-08 multi-chat, Phase 3).

Extracted from CLIENT/app/websocket/handler.py: everything that used to run INLINE in the
UI WebSocket receive loop (dispatch → retry → post-processing) now runs as an independent
asyncio task owned by a ChatChannel. That is what makes "switch chat, keep generating" work:
the UI socket can come and go while this task streams through its ChannelWebSocket into the
channel (live push or bounded background buffer).

The flow mirrors the pre-refactor handler exactly:
  build dispatch closure (agentic | normal) → retry_with_backoff (20 min budget) →
  cancel handling / error frames / assistant persistence / search suggestion / done.
"""

import asyncio
import json
import logging
import time

from app.chat_bus.channel import ChannelStatus, ChannelWebSocket
from app.chat_bus.exec_approval import ExecApprovalState
from app.websocket.message_types import (
    send_recovery_warning,
    send_system,
    send_error,
    send_provider_error,
    send_execution_fail,
    send_search_web_suggestion,
    send_done,
)
from app.websocket.db_ops import (
    save_assistant_message,
    save_user_message,
    _sync_conversation_working_root,
)
from app.db_manager import get_db_connection, close_db_connection
from logic import normal_mode, agentic_mode, build_normal_config, build_agentic_config
from logic.base_mode import SessionTimer  # (2026-08-23) active-time session timer (pauses on user menus)
from logic.normal import get_ollama_status_message
import app.prompts as _app_prompts
from utils.retry import retry_with_backoff

logger = logging.getLogger("COOLEMS.ChatBus")


def _is_user_cancelled(error) -> bool:
    """Check if an error/result is a user-initiated cancellation (same rules as the old handler)."""
    if not isinstance(error, BaseException):
        return False
    name = type(error).__name__
    if "cancel" in name.lower():
        return True
    if name == "WebSocketDisconnect":
        return True
    if name in ("ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError"):
        return True
    return False


async def _reject_pending_steers(channel, sink, message: str, code: str) -> int:
    """Settle any steering message the loop never reached (2026-10-23).

    A steer that is still queued when the turn ends can never be applied. The UI bubble
    for it must not stay stuck on "queued", so we drain the channel and tell attached
    clients why. Best-effort: a dead channel must never break the turn teardown.
    """
    try:
        pending = channel.drain_steer()
    except Exception:
        pending = []
    for item in pending:
        try:
            await sink.send_text(json.dumps({
                "type": "steer_rejected",
                "steer_id": item.get("id") if isinstance(item, dict) else None,
                "content": item.get("text", "") if isinstance(item, dict) else "",
                "code": code,
                "message": message,
            }))
        except Exception:
            pass  # UI gone - nothing to update
    return len(pending)


class _StatusSink:
    """Thin wrapper over ChannelWebSocket that keeps channel.status honest.

    The SERVER tells us when a queued request actually starts (queue_start frame) and the
    turn ends (done/error/cancelled); mapping those onto ChannelStatus drives the sidebar
    dot without any extra protocol work on the UI side.
    """

    def __init__(self, channel, inner: ChannelWebSocket):
        self._channel = channel
        self._inner = inner

    async def send_text(self, data):
        try:
            import json as _json
            msg = _json.loads(data)
            t = msg.get("type") if isinstance(msg, dict) else None
        except (ValueError, TypeError):
            t = None
        if t == "queue_status":
            self._channel.queue_position = int(msg.get("position", 0) or 0)
            self._channel.set_status(ChannelStatus.QUEUED)
        elif t == "queue_start":
            self._channel.set_status(ChannelStatus.GENERATING)
        elif t in ("done",):
            pass  # status set by the runner after post-processing
        await self._inner.send_text(data)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def run_generation_turn(channel, channel_ws: ChannelWebSocket, *, db_path: str,
                              provider, agent, tool_orchestrator, ws_key: str | None,
                              stop_events: dict, prepared: dict, api_timeout: int):
    """Run ONE chat turn to completion in the background.

    *prepared* is exactly what the old handler's _prepare_user_message() returned; this
    function owns everything after that point (dispatch + retry + persistence). Never
    raises — every failure becomes a frame on the channel plus a log line.
    """
    conv_id = channel.conv_id
    stop_event = channel.stop_event
    sink = _StatusSink(channel, channel_ws)

    model = prepared["model"]
    agent_mode = prepared["agent_mode"]
    enable_thinking = prepared["enable_thinking"]
    enhanced_message = prepared["enhanced_message"]
    image_data = prepared["image_data"]
    image_paths = prepared["image_paths"]
    file_contents = prepared["file_contents"]
    media_files = prepared["media_files"]
    requires_tools = prepared["requires_tools"]
    ollama_tools = prepared["ollama_tools"]
    dna_content = prepared["dna_content"]
    user_msg = prepared["user_msg"]

    # fresh state for this turn (the channel outlives it)
    stop_event.clear()
    channel_ws.reset_capture()
    channel._answer_persisted = False   # (2026-09-09 audit) fresh flag for this turn

    # (2026-10-23 live steering) Only an agentic turn has a next step to inject into.
    # Normal one-shot completions are refused by ChatBus.steer() with a clear message.
    channel.agent_mode = bool(agent_mode)
    # Any steering message queued for a PREVIOUS (superseded) turn can never run now.
    # Settle its UI bubble before this new turn starts streaming.
    try:
        await _reject_pending_steers(
            channel, sink,
            "A newer message replaced the task before this steering message could be applied.",
            "task_superseded",
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[CHATBUS] stale-steer cleanup failed (non-fatal): {e}")

    async def _persist_applied_steer(item) -> None:
        """Persist one applied steering message in the conversation history.

        Called by logic.react_loop ONLY after the loop actually injected the text into the
        provider message list. Raw text is stored (no injection header) so reloading the
        conversation shows exactly what the user typed, in chronological order before the
        final assistant answer.
        """
        text = item.get("text", "") if isinstance(item, dict) else str(item or "")
        if not text.strip():
            return
        try:
            save_user_message(db_path, conv_id, text, auto_title=False)
        except Exception as e:
            logger.error(f"[CHATBUS] Failed to persist steering message for conv={conv_id}: {e}")

# (2026-09-24 python_exec user approval): a FRESH approval state per run - any
    # "run until task done" grant from an earlier loop is gone by design, so the next
    # python_exec in this new loop asks the user again. The channel holds it for the
    # whole turn; ToolExecutor picks it up via conversation_id -> ChatBus.
    channel.exec_approval = ExecApprovalState()

    # (2026-08-23 session stats) FRESH active-time timer for THIS turn. It counts only
    # real working time: ToolExecutor pauses it while a user-decision menu is open
    # (python_exec approval / URL consent), so the reported 'current session time' in
    # the UI never includes the minutes the AI spends waiting for a click. The previous
    # turn's settled duration rides along as last_session_sec for the 'last session' stat.
    session_timer = SessionTimer()
    session_timer.start()
    last_session_sec = float(getattr(channel, "last_session_duration_sec", 0.0) or 0.0)

    try:
        conversation_history = list(prepared.get("history", []))
        final_response = ""

        async def _dispatch_ai_request():
            """Dispatch to the correct mode (extracted verbatim from the old handler)."""
            if agent_mode:
                agent_config = build_agentic_config(
                    model=model,
                    enable_thinking=enable_thinking,
                    provider=provider,
                    agent_name=agent.get_name(),
                    dna_content=dna_content,
                    generic_tool_prompt=_app_prompts.GENERIC_TOOL_PROMPT,
                    image_data=image_data,
                    image_paths=image_paths,
                    file_contents=file_contents,
                    websocket=sink,  # streamed content captured for partial persistence + routed via channel
                    conversation_id=conv_id,
                    stop_event=stop_event,
                    tool_orchestrator=tool_orchestrator,
                    ollama_tools=ollama_tools,
                    api_key=ws_key,
                    session_timer=session_timer,
                        last_session_sec=last_session_sec,
                        # (2026-10-23 live steering) the running ReAct loop drains this
                        # channel's queue at each safe boundary; the callback persists each
                        # message only after it was actually injected.
                        steer_source=channel,
                        on_steer_applied=_persist_applied_steer,
                    )
                result = await agentic_mode(agent_config, conversation_history, enhanced_message, media_files)
                conversation_history.append({"role": "assistant", "content": result})
                return result

            normal_config = build_normal_config(
                model=model,
                enable_thinking=enable_thinking,
                provider=provider,
                agent_name=agent.get_name(),
                dna_content=dna_content,
                image_data=image_data,
                image_paths=image_paths,
                file_contents=file_contents,
                websocket=sink,
                conversation_id=conv_id,
                stop_event=stop_event,
                session_timer=session_timer,
                last_session_sec=last_session_sec,
            )
            return await normal_mode(normal_config, conversation_history, enhanced_message, media_files)

        try:
            final_response, succeeded = await retry_with_backoff(
                func=_dispatch_ai_request,
                websocket=sink,
                stop_event=stop_event,
                provider_name=provider.name,
                auto_restart_provider=provider,
                recovery_callback=send_recovery_warning,
            )

            # --- user cancellation: graceful stop (same behaviour as the old handler) ---
            if _is_user_cancelled(final_response):
                logger.info(f"[CHATBUS] User cancelled generation for conv={conv_id}")
                try:
                    await send_system(sink, "Generation stopped. Type new message to continue.")
                except Exception:
                    pass
                final_response = ""

                # FIX (2026-08-30): keep the working_root sync on cancel (carried over verbatim)
                try:
                    conn = get_db_connection()
                    try:
                        _sync_conversation_working_root(conn.cursor(), conv_id)
                        conn.commit()
                    finally:
                        close_db_connection(conn)
                except Exception as sync_err:
                    logger.warning(f"[CHATBUS] working_root sync after cancel failed: {sync_err}")

                channel.set_status(ChannelStatus.CANCELLED)
                await _reject_pending_steers(
                    channel, sink,
                    "The task was stopped before this steering message could be applied.",
                    "task_stopped",
                )
                try:
                    await send_done(sink)
                except Exception:
                    pass
                return

            if not succeeded:
                # All retries exhausted or non-retryable error (NOT user cancellation)
                logger.error(f"[CHATBUS] Provider error after retries for conv={conv_id}: {final_response}")
                status_msg = get_ollama_status_message(final_response, model, api_timeout, provider=provider)
                try:
                    await send_provider_error(sink, type(final_response).__name__, str(final_response))
                    await send_execution_fail(sink, -1, type(final_response).__name__, str(final_response))
                except Exception as e:
                    logger.debug(f"[CHATBUS] error frames not delivered (UI may be gone): {e}")
                final_response = status_msg
                channel.set_status(ChannelStatus.ERROR)

        except Exception as e:
            logger.error(f"[CHATBUS] Unexpected error for conv={conv_id}: {e}", exc_info=True)
            final_response = f"**Unexpected Error**\n\nAn unexpected error occurred: {str(e)}"
            try:
                await send_execution_fail(sink, -1, type(e).__name__, str(e))
            except Exception:
                pass
            channel.set_status(ChannelStatus.ERROR)

        # --- persist the assistant answer (the bus guarantees this even when nobody was looking) ---
        if final_response and not stop_event.is_set():
            try:
                save_assistant_message(db_path, conv_id, final_response)
                channel._answer_persisted = True  # (2026-09-09 audit) answer is now in the DB
                agent.learn_lesson(
                    f"Completed interaction: {user_msg[:50]}...",
                    "Handled user request successfully",
                )
            except Exception as e:
                logger.error(f"[CHATBUS] Failed to persist assistant message for conv={conv_id}: {e}")

            # (2026-09-08 multi-chat UI fix): mark the persisted answer on the wire. A re-attaching
            # client replays this frame BEFORE its replay_end marker, so it knows the streamed text
            # of this turn is already in the DB history and must not be rendered again (no duplicate
            # bubble) while still showing that a generation happened here.
            try:
                await sink.send_text(json.dumps({
                    "type": "answer_persisted",
                    "content_length": len(final_response),
                }))
            except Exception:
                pass  # dead channel — the frame is buffered or dropped, never fatal

        # --- search suggestion for plain (non-tool) queries, same as the old handler ---
        if not requires_tools and not agent_mode:
            try:
                await send_search_web_suggestion(sink, user_msg, "Search same question on internet")
            except Exception as ws_err:
                logger.warning(f"[CHATBUS] search suggestion not delivered: {ws_err}")

        # (2026-10-23 live steering) Anything still queued here arrived too late for the
        # loop to inject - settle the UI bubble instead of leaving it on "queued".
        await _reject_pending_steers(
            channel, sink,
            "The task finished before this steering message could be applied. Send it as a new message.",
            "task_ended",
        )

        channel.set_status(ChannelStatus.DONE)
        try:
            await send_done(sink)
        except Exception:
            pass

    finally:
        # (2026-08-23 session stats) settle this turn's ACTIVE time on EVERY exit path
        # (done / cancel / error - the early 'return' after a user Stop included) and hand it
        # to the NEXT turn as 'last session'. Paused time (approval menus) is excluded.
        try:
            _elapsed = float(session_timer.stop())
            channel.last_session_duration_sec = round(_elapsed, 1)
            logger.info(f"[CHATBUS] Session time settled for conv={conv_id}: {_elapsed:.1f}s active (approval-menu pauses excluded)")
        except Exception as e:  # pragma: no cover - stats must never break the turn
            logger.debug(f"Session timer settle failed for conv={conv_id}: {e}")


def register_stop_event(stop_events: dict, conv_id: str, event: asyncio.Event) -> None:
    """Keep the legacy stop_events dict in sync so /api/stop/{conv_id} keeps working."""
    stop_events[conv_id] = {"event": event, "created_at": time.time()}


def unregister_stop_event(stop_events: dict, conv_id: str, event: asyncio.Event) -> None:
    """Remove the entry ONLY when it still holds THIS channel's event (CANCEL FIX 2026-09-07)."""
    entry = stop_events.get(conv_id)
    if entry is not None:
        ev = entry.get("event") if isinstance(entry, dict) else entry
        if ev is event or ev is None:
            del stop_events[conv_id]
