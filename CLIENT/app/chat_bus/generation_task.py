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
from app.websocket.db_ops import save_assistant_message, _sync_conversation_working_root
from app.db_manager import get_db_connection, close_db_connection
from logic import normal_mode, agentic_mode, build_normal_config, build_agentic_config
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

    # (2026-09-24 python_exec user approval): a FRESH approval state per run - any
    # "run until task done" grant from an earlier loop is gone by design, so the next
    # python_exec in this new loop asks the user again. The channel holds it for the
    # whole turn; ToolExecutor picks it up via conversation_id -> ChatBus.
    channel.exec_approval = ExecApprovalState()
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

    channel.set_status(ChannelStatus.DONE)
    try:
        await send_done(sink)
    except Exception:
        pass


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
