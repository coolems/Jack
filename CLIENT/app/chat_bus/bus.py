"""ChatBus — registry of per-chat background sessions (2026-09-08 multi-chat, Phase 3).

One ChatChannel per conversation id; each channel owns its generation task, stop event and
frame buffer. The UI WebSocket handler becomes a thin subscriber: it attaches/detaches its
socket to the channel instead of owning the whole turn inline. That is what lets several
chats generate at once — switching chats in the browser no longer kills anything.

Responsibilities (and ONLY these):
  * get_or_create / submit   — create or reuse a channel, spawn the background task
  * attach / detach          — UI socket lifecycle per conversation
      * steer                    — queue a mid-run user message for the live agentic turn
      * stop                     — cancel queued-or-running work for one chat (/api/stop)
  * stop_all                 — cancel EVERY live session (UI closed / backend shutdown)
  * stale cleanup            — drop channels idle too long (replaces the old dict cleanup)

STALE-TASK CONTRACT (2026-09-10 fix, "No active generation found" bug):
  The UI's red stop button and the window-close path both rely on this bus. A chat that is
  generating/queued ALWAYS has a live channel with a running task; /api/stop routes through
  it (see routers/agent.py) so the button can never report "No active generation found" for
  a chat that visibly shows as working.

  stop() uses two layers:
    1. cooperative — set the channel's stop_event; every loop boundary, retry wait and the
       relay-WS watcher react to it (graceful unwind + 'done' frame);
    2. hard backstop — if the turn has not unwound within STOP_HARD_CANCEL_GRACE_SEC the
       task is cancelled anyway, so "Stop" ALWAYS stops even mid long tool execution.

  stop_all() implements the other half of the user's contract: when the UI window closes or
  crashes (POST /api/shutdown-all), EVERY task is stopped - no zombie turns keep streaming
  (and burning tokens) in the background after the window is gone.
"""

import asyncio
import json
import logging
import time
from typing import Dict

from .channel import ChatChannel, ChannelStatus, ChannelWebSocket
from .generation_task import run_generation_turn, register_stop_event, unregister_stop_event
from app.websocket.db_ops import save_user_message
from logic.runtime_notice import NOTICE_QUESTION, render_notice

from config import NOTICE_QUIET_WAKE

logger = logging.getLogger("COOLEMS.ChatBus")

# Hard-cancel backstop: after this many seconds a stop-requested turn that has not unwound
# on its own (e.g. stuck inside a long tool execution) is force-cancelled. Keep it short -
# the graceful path normally wins within milliseconds to a couple of seconds.
STOP_HARD_CANCEL_GRACE_SEC = 5.0


class ChatBus:
    """conv_id -> ChatChannel registry with background generation tasks."""

    def __init__(self, *, db_path: str, provider, model_name: str, api_timeout: int,
                 agent, tool_orchestrator, stop_events: dict,
                 stop_grace_sec: float | None = None):
        self._db_path = db_path
        self._provider = provider
        self._model_name = model_name
        self._api_timeout = api_timeout
        self._agent = agent
        self._tool_orchestrator = tool_orchestrator
        # legacy stop_events dict stays the source of truth for /api/stop (kept in sync)
        self._stop_events = stop_events
        # tests may shrink this to keep suites fast; production keeps the default
        self._stop_grace_sec = float(stop_grace_sec if stop_grace_sec is not None else STOP_HARD_CANCEL_GRACE_SEC)

        self._channels: dict[str, ChatChannel] = {}
        self._ws_key: str | None = None      # set by the first authenticated connection
        self._cleanup_task: asyncio.Task | None = None
        # (2026-09-10 stale-task fix) The event loop that owns this bus' generation tasks.
        # stop_all() may be called from a DIFFERENT thread/loop (e.g. the uvicorn shutdown
        # path or a foreign asyncio.run in tests); task.cancel() is cross-loop safe but
        # awaiting the unwind is not - when called off-loop we schedule the awaits on the
        # owner loop instead of deadlocking/corrupting state.
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    # --- context ------------------------------------------------------------------

    def set_ws_key(self, ws_key) -> None:
        """Remember the authenticated API key (used for role-based tool filtering)."""
        if ws_key and self._ws_key is None:
            self._ws_key = ws_key

    @property
    def channels(self) -> dict[str, ChatChannel]:
        return self._channels

    # --- owner loop (cross-thread safe stop_all) --------------------------------------

    def _bind_owner_loop(self) -> None:
        """Remember the event loop that runs this bus' generation tasks.

        Called from hot paths (attach/submit) so the binding is done by the FIRST real
        activity on the main uvicorn loop - before any shutdown or foreign-thread call
        can happen. No-op once bound."""
        if self._owner_loop is None:
            try:
                self._owner_loop = asyncio.get_running_loop()
                logger.debug("[CHATBUS] Owner event loop bound (stop_all cross-loop support)")
            except RuntimeError:
                pass  # no running loop here - will bind on next hot-path call

    async def stop_all(self, reason: str = "ui-closed") -> dict:
        """Stop EVERY live chat session (all tasks). Returns a summary dict.

        Used when the UI closes/crashes or the CLIENT backend shuts down: the user's
        contract is that no task keeps running once the window goes away. Each channel
        gets its stop event set (cooperative abort at every loop boundary) AND its task
        cancelled immediately (hard backstop for tool-execution windows that do not poll
        the event). Unwinds are awaited with a bounded timeout so this never hangs.

        Cross-loop safe: called from another thread/loop it schedules the per-channel
        awaits on the owner loop and waits with a bounded timeout instead of deadlocking.
        """
        self._bind_owner_loop()
        live = [ch for ch in list(self._channels.values())
                # (2026-11-30) A channel with no TURN task may still own delegated work -
                # the window-closing contract covers that too, so it is not skipped.
                if (ch.task is not None and not ch.task.done())
                or self._live_runtime_count(ch.conv_id) > 0]

        async def _stop_one(ch: ChatChannel) -> bool:
            # Fail any steering message still waiting for an injection point BEFORE the
            # stop notification, so the UI can settle its bubble honestly.
            try:
                await self._reject_pending_steers(
                    ch,
                    "The task was stopped before this steering message could be applied.",
                    "task_stopped",
                )
            except Exception:
                pass
            try:
                await ch.emit_frame({"type": "system",
                                     "content": f"Generation stopped ({reason}). Type new message to continue."})
            except Exception:
                pass
            if not ch.stop_event.is_set():
                ch.stop_event.set()
            # (2026-12) The user's contract for stop_all is "no task keeps running once
            # the window is gone" - that includes delegated children and background jobs.
            try:
                await self._cascade_stop(ch, reason=f"runtime shutdown ({reason})")
            except Exception:
                pass
            task = ch.task
            try:
                if task is not None and not task.done():
                    task.cancel()
                    # Observe the unwind WITHOUT swallowing our own cancellation:
                    # asyncio.wait reports the observed task's state instead of raising it.
                    await asyncio.wait({task}, timeout=self._stop_grace_sec + 1.0)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"[CHATBUS] stop_all wait failed for conv={ch.conv_id}: {e}")
            if ch.status not in (ChannelStatus.CANCELLED, ChannelStatus.ERROR):
                ch.set_status(ChannelStatus.CANCELLED)
            return True

        stopped = 0
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if self._owner_loop is not None and running is not self._owner_loop:
            # Foreign thread/loop (e.g. test harness, or a shutdown path that lost its loop):
            # hop onto the owner loop for the awaits; bounded wait so we never hang a caller.
            async def _run_all():
                nonlocal stopped
                for ch in live:
                    if await _stop_one(ch):
                        stopped += 1

            fut = asyncio.run_coroutine_threadsafe(_run_all(), self._owner_loop)
            try:
                fut.result(timeout=5.0 + (self._stop_grace_sec + 1.0) * len(live))
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[CHATBUS] stop_all cross-loop wait failed: {e}")
        else:
            for ch in live:
                if await _stop_one(ch):
                    stopped += 1

        logger.info(f"[CHATBUS] stop_all({reason}): stopped {stopped} live session(s) "
                    f"of {len(self._channels)} channel(s)")
        return {"stopped": stopped, "total_channels": len(self._channels), "reason": reason}

    # --- channel lifecycle -----------------------------------------------------------

    def get_or_create(self, conv_id: str) -> ChatChannel:
        ch = self._channels.get(conv_id)
        if ch is None:
            ch = ChatChannel(conv_id)
            self._channels[conv_id] = ch
            logger.debug(f"[CHATBUS] Channel created for conv={conv_id}")
        return ch

    def attach(self, ws, conv_id: str):
        """Attach a UI socket to its channel; returns the channel."""
        self._bind_owner_loop()
        ch = self.get_or_create(conv_id)
        ch.attach(ws)
        logger.debug(f"[CHATBUS] UI attached to conv={conv_id} "
                     f"(status={ch.status.value}, buffered={len(ch.buffered_frames())})")
        return ch

    def detach(self, ws, conv_id: str) -> None:
        """Detach a UI socket. The channel (and any running generation) keeps living."""
        ch = self._channels.get(conv_id)
        if ch is not None:
            last = ch.detach(ws)
            logger.debug(f"[CHATBUS] UI detached from conv={conv_id} "
                         f"(status={ch.status.value}, still_attached=not {last})")

    # --- generation -------------------------------------------------------------------

    async def submit(self, ws, conv_id: str, prepared: dict,
                     notice_wake: bool = False) -> ChatChannel:
        """Start a background generation turn for *prepared* (see handler._prepare_user_message).

        If the chat already has a live session the old one is stopped first — a new user
        message supersedes it (same as clicking send while generating before this refactor,
        which used to be impossible because the receive loop was busy).

        notice_wake=True marks a turn started by the RUNTIME (a settlement notice on an
        idle chat) rather than by the user. Such a turn does NOT reset the consecutive-wake
        budget - it consumes it - which is what stops the agent's own children from waking
        it forever. A real user message always resets it.
        """
        self._bind_owner_loop()
        ch = self.get_or_create(conv_id)

        if ch.task is not None and not ch.task.done():
            logger.info(f"[CHATBUS] New message supersedes live session for conv={conv_id} — stopping it")
            # cascade=False: a NEW USER MESSAGE supersedes the TURN, not the background
            # work the user deliberately started. Children and jobs outlive the turn that
            # launched them - that is the whole point of a background job. Only an
            # explicit Stop (or shutdown) cascades.
            await self.stop(conv_id, send_frame=False, cascade=False)

        # (2026-12 notice lane) A real user message re-opens the notice wake budget. A
        # notice-driven turn does NOT reset it - it consumes it, and the consumption is
        # counted in submit_notice() where the wake decision is made.
        if not notice_wake:
            ch.reset_notice_wakes()

        channel_ws = ChannelWebSocket(ch)
        register_stop_event(self._stop_events, conv_id, ch.stop_event)
        ch.set_status(ChannelStatus.GENERATING)  # may be corrected to QUEUED by queue_status frames
        # (2026-10-23 live steering) mark the mode BEFORE the task starts, so a steer sent
        # in the tiny window between submit() and run_generation_turn() is not refused.
        ch.agent_mode = bool(prepared.get("agent_mode"))
        prepared = dict(prepared)
        prepared["history"] = list(prepared.get("history", []))

        task = asyncio.create_task(
            self._guarded_turn(ch, channel_ws, prepared),
            name=f"chat-turn-{conv_id[:8]}",
        )
        ch.task = task
        return ch

    async def steer(self, conv_id: str, text: str, steer_id: str | None = None):
        """Queue a mid-run user message for this chat's LIVE agentic turn.

        Live steering (2026-10-23): the UI lets the user keep typing while the agent works;
        pressing Enter sends the text here instead of starting a new turn. The running ReAct
        loop drains this queue at its next safe boundary and injects the text as a fresh user
        instruction (see logic/react_loop._apply_pending_steers), so the current task continues
        with the correction instead of being killed and restarted.

        Returns a 3-tuple ``(ok, info, code)``:
          * ok=True  -> info is the queued item dict (its "id" is echoed to the UI)
          * ok=False -> info is a human-readable reason, code a stable machine code.

        Refused when there is no live turn or the turn is not agentic: a normal one-shot
        completion has no next step to inject into, and the UI is told honestly instead of
        silently dropping the user's text.
        """
        self._bind_owner_loop()
        text = (text or "").strip()
        if not text:
            return False, "The steering message is empty.", "empty"

        ch = self._channels.get(conv_id)
        if ch is None or ch.task is None or ch.task.done():
            return (False,
                    "The task already finished - send this as a normal message.",
                    "no_active_turn")
        if not getattr(ch, "agent_mode", False):
            return (False,
                    "Steering is only available while an Agent-mode task is running. "
                    "Use Stop to interrupt this response.",
                    "not_agentic")
        if ch.stop_event.is_set():
            return (False,
                    "This task is being stopped - send the message as a new turn instead.",
                    "task_stopping")

        item = ch.queue_steer(text, steer_id)
        # A real user message (typed mid-run) re-opens the notice wake lane: the user is
        # watching this chat, so its own settlements may wake it again afterwards.
        ch.reset_notice_wakes()
        logger.info(f"[CHATBUS] Steering message queued for conv={conv_id} "
                    f"(id={item['id'][:12]}, pending={ch.pending_steer_count()})")
        return True, item, ""

    # --- runtime notices (2026-12 subagents / background jobs / questions) ----------

    async def queue_notice(self, conv_id: str, text: str, source: str = "runtime",
                           notice_id: str | None = None, meta: dict | None = None) -> dict:
        """Queue one runtime notice on a chat's channel (UI frame emitted, no turn).

        Used when the chat is LIVE: the ReAct loop injects it at its next safe boundary.
        Also used when a wake is refused (budget spent) — the notice then waits for the
        user's next turn instead of starting one.
        """
        self._bind_owner_loop()
        ch = self.get_or_create(conv_id)
        item = ch.queue_notice(text, source, notice_id, meta)
        try:
            await ch.emit_frame({
                "type": "notice_queued",
                "conv_id": conv_id,
                # The notice belongs to the turn that is running right now, so the UI can
                # place its row inside that turn's block (same targeting as steering).
                "turn_id": getattr(ch, "turn_id", "") or "",
                "notice_id": item["id"],
                "source": item["source"],
                "content": item["text"][:600],
                "at_ts": item["received_at"],
                "pending": ch.pending_notice_count(),
            })
        except Exception:
            pass  # UI gone - the notice still waits
        logger.info(f"[CHATBUS] Notice queued for conv={conv_id} "
                    f"(source={item['source']}, id={item['id'][:12]}, pending={ch.pending_notice_count()})")
        return item

    async def submit_notice(self, conv_id: str, text: str, source: str = "runtime",
                            meta: dict | None = None) -> bool:
        """Wake an IDLE chat with a runtime notice: a follow-up turn with no user input.

        This is the lane that makes background work useful: a child or a job that settles
        while nobody is talking still gets acted on. Bounded by the channel's consecutive-
        wake budget, because the notices are generated by the agent's own work — without a
        cap one settlement wakes the parent, which delegates again, forever.

        Returns True when a turn was started, False when the notice was only queued
        (live turn, wake budget spent, quiet-wake setting, or a stopping channel).
        """
        self._bind_owner_loop()
        text = (text or "").strip()
        if not text:
            return False
        ch = self.get_or_create(conv_id)

        if ch.task is not None and not ch.task.done():
            # A live turn already has an injection point — do not start a second turn.
            await self.queue_notice(conv_id, text, source, meta=meta)
            return False
        if ch.stop_event.is_set():
            await self.queue_notice(conv_id, text, source, meta=meta)
            return False
        if NOTICE_QUIET_WAKE or not ch.notice_wake_allowed():
            item = await self.queue_notice(conv_id, text, source, meta=meta)
            try:
                await ch.emit_frame({
                    "type": "notice_deferred",
                    "conv_id": conv_id,
                    "notice_id": item["id"],
                    "source": item["source"],
                    "content": item["text"][:600],
                    "reason": ("quiet wake mode" if NOTICE_QUIET_WAKE
                               else "notice wake budget spent - waiting for your next message"),
                    "at_ts": item["received_at"],
                })
            except Exception:
                pass
            logger.info(f"[CHATBUS] Notice deferred for conv={conv_id} (source={source})")
            return False

        # The wake is decided HERE, so the budget is spent here - not inside submit().
        # A notice-driven turn must consume one unit even if submit() is replaced (tests,
        # or a future wrapper): otherwise the cap would never be reached and one settled
        # child could wake the parent forever.
        ch.bump_notice_wakes()

        # Persist the notice BEFORE the turn runs, flagged is_notice: the UI never paints
        # it as a chat bubble (it is a row of the turn's block), and a later turn loading
        # history still sees WHO said it (the header rides in the row).
        try:
            save_user_message(self._db_path, conv_id,
                              render_notice(source, text), auto_title=False,
                              is_notice=True)
        except Exception as e:
            logger.error(f"[CHATBUS] Failed to persist notice for conv={conv_id}: {e}")

        ollama_tools = None
        if self._tool_orchestrator is not None:
            try:
                ollama_tools = await self._tool_orchestrator.get_ollama_tools(api_key=self._ws_key)
            except Exception as e:
                logger.warning(f"[CHATBUS] Notice wake: tool schema unavailable ({e})")

        # History for a wake turn is loaded from the DB exactly like a user turn: the
        # notice itself is already persisted (is_notice), so the model sees the session
        # plus this notice as its newest message.
        history: list = []
        try:
            from utils.history_manager import (
                load_conversation_history,
                trim_history_to_token_limit,
            )
            from config import max_history_tokens
            history = load_conversation_history(conv_id)
            history = trim_history_to_token_limit(history, max_tokens=max_history_tokens())
        except Exception as e:
            logger.warning(f"[CHATBUS] Notice wake: history load failed ({e}) - running with no history")

        prepared = {
            "user_msg": text,
            "model": self._model_name,
            "system_prompt": f"You are {self._agent.get_name()}.",
            "agent_mode": True,          # a notice is only meaningful inside the loop
            "enable_thinking": False,
            "enhanced_message": render_notice(source, text),
            "image_data": [],
            "image_paths": [],
            "file_contents": [],
            "media_files": [],
            "requires_tools": True,
            "ollama_tools": ollama_tools,
            "dna_content": self._agent.get_identity(),
            "notice_wake": True,         # the turn runner must not persist a user message
            "history": history,
        }
        logger.info(f"[CHATBUS] Notice wake turn for conv={conv_id} (source={source}, "
                    f"wake_round={ch._wake_rounds})")
        await self.submit(None, conv_id, prepared, notice_wake=True)
        return True

    async def deliver_notice(self, conv_id: str, text: str, source: str = "runtime",
                             meta: dict | None = None) -> bool:
        """Route one runtime notice: live turn -> inject at its next boundary; idle -> wake.

        The single entry point every settlement handler uses (jobs, children, questions).
        Returns True when a follow-up turn was started.
        """
        ch = self._channels.get(conv_id)
        if ch is not None and ch.task is not None and not ch.task.done():
            await self.queue_notice(conv_id, text, source, meta=meta)
            return False
        return await self.submit_notice(conv_id, text, source, meta=meta)

    async def answer_question(self, conv_id: str, request_id: str, answer: str) -> bool:
        """Deliver the user's answer to an ask_user_question card.

        Live turn -> a <question_answered> notice injected at the next boundary (the tool
        call that asked may already have returned 'pending' in timed mode). Idle chat ->
        a follow-up turn, so a late answer still lands. The pending card is dropped either
        way, so the UI cannot answer the same question twice.
        """
        ch = self._channels.get(conv_id)
        if ch is None:
            return False
        answer = (answer or "").strip()
        # The card is settled whether or not the answer reached the model: leaving it
        # pending would let the UI answer the same question twice.
        ch.drop_pending_question(request_id)
        if not answer:
            return False
        return await self.deliver_notice(conv_id, answer, NOTICE_QUESTION,
                                         meta={"request_id": request_id})

    async def expire_open_questions(self, conv_id: str) -> list[str]:
        """Close this chat's still-open question cards when the turn ends.

        The awaiting tool call gets 'unanswered' (never an invented answer), the pending
        store is dropped, and the request ids come back so the caller can remove each
        card. Best-effort: a dead channel returns [].
        """
        ch = self._channels.get(conv_id)
        if ch is None:
            return []
        ids: list[str] = []
        try:
            state = getattr(ch, "exec_approval", None)
            if state is not None and hasattr(state, "expire_questions"):
                ids = state.expire_questions("unanswered") or []
        except Exception as e:  # pragma: no cover - teardown must never raise
            logger.debug(f"[CHATBUS] question expire failed for conv={conv_id}: {e}")
        for item in list(ch.pending_questions()):
            ch.drop_pending_question(item.get("id"))
        return ids
    async def _reject_pending_steers(self, ch: ChatChannel, message: str, code: str) -> int:
        """Tell attached UI clients that still-queued steering messages will never run.

        Used on stop/stop_all so a pending steer bubble cannot stay stuck at "queued"
        forever when the turn is torn down before the loop reached an injection point.
        Best-effort: a dead channel must never break the stop path.
        """
        pending = ch.drain_steer()
        turn_id = str(getattr(ch, "turn_id", "") or "")
        for item in pending:
            try:
                await ch.emit_frame({
                    "type": "steer_rejected",
                    "steer_id": item.get("id"),
                    # The steer belongs to the turn that was running when it was typed -
                    # the UI settles that turn's block row, never a chat bubble.
                    "turn_id": turn_id,
                    "content": item.get("text", ""),
                    # Placement time (when the user typed it), same as steer_applied.
                    "at_ts": item.get("received_at"),
                    "code": code,
                    "message": message,
                })
                # The loop-history row too, so a steer that never ran still reads in the
                # turn's block as "not applied" instead of disappearing.
                await ch.emit_frame({
                    "type": "cycle_steer",
                    "conv_id": ch.conv_id,
                    "turn_id": turn_id,
                    "steer_id": item.get("id"),
                    "text": item.get("text", ""),
                    "applied": False,
                    "rejected": True,
                    "reason": message,
                    "step_no": 0,
                    "at_ts": item.get("received_at"),
                })
            except Exception:
                pass  # UI gone - nothing to update
        return len(pending)

    async def _guarded_turn(self, ch: ChatChannel, channel_ws: ChannelWebSocket, prepared: dict) -> None:
        """Run the turn; on completion clean up the legacy stop-event entry."""
        try:
            await run_generation_turn(
                ch, channel_ws,
                db_path=self._db_path,
                provider=self._provider,
                agent=self._agent,
                tool_orchestrator=self._tool_orchestrator,
                ws_key=self._ws_key,
                stop_events=self._stop_events,
                prepared=prepared,
                api_timeout=self._api_timeout,
            )
        except asyncio.CancelledError:
            ch.set_status(ChannelStatus.CANCELLED)
            # (2026-09-10 stale-task fix) a HARD-cancelled turn dies here without the
            # graceful unwind of run_generation_turn - still settle attached UI clients by
            # emitting the terminal 'done' frame, so no tab is left with a stuck red button.
            try:
                await channel_ws.send_text(json.dumps({"type": "done"}))
            except Exception:
                pass  # second cancellation or dead channel - nothing more we can do
            raise
        except Exception as e:  # pragma: no cover - defensive: turn must never die silently
            logger.error(f"[CHATBUS] Turn crashed for conv={ch.conv_id}: {e}", exc_info=True)
            ch.set_status(ChannelStatus.ERROR)
        finally:
            unregister_stop_event(self._stop_events, ch.conv_id, ch.stop_event)
            # (2026-09-09 audit fix): drop the buffered tail once a turn's answer is PERSISTED.
            # Replay only matters for an IN-FLIGHT turn: while it streams with nobody attached,
            # its chunks are the ONLY copy (the DB gets them later). Once save_assistant_message
            # has run, a re-attaching UI reloads that answer from the DB history anyway, so stale
            # frames left here would be replayed and rendered as a DUPLICATE assistant bubble before
            # the chat_status frame marks the chat idle. Two guards:
            #   * ch._answer_persisted - only set by THIS turn after successful persistence, so a
            #     finished-but-unpersisted turn (error/cancel) keeps its replayable tail;
            #   * task ownership - bus.submit() supersedes a live turn WITHOUT awaiting the old
            #     task's unwind, and the new turn streams into the SAME buffer: only clear when no
            #     newer task has taken over this channel in the meantime.
            if ch._answer_persisted and ch.task is asyncio.current_task():
                ch.clear_buffer()

    # --- cancellation -------------------------------------------------------------------

    def _live_runtime_count(self, conv_id: str) -> int:
        """How much delegated/background work this chat still owns (children + jobs).

        Read lazily so a bus built without the runtime (tests, legacy callers) still works.
        Used by stop()/stop_all() to decide there IS something to stop even when the chat's
        TURN task is gone - delegated work outlives the turn that started it.
        """
        count = 0
        try:
            from app.runtime.children import get_child_registry
            reg = get_child_registry()
            if reg is not None:
                count += len(reg.live_children(conv_id))
        except Exception:
            pass
        try:
            from app.runtime.jobs import get_job_registry
            jobs = get_job_registry()
            if jobs is not None:
                count += len(jobs.live_for_owner(conv_id))
        except Exception:
            pass
        return count

    async def _cascade_stop(self, ch: ChatChannel, reason: str) -> Dict[str, int]:
        """Stop this chat's delegated children AND background jobs, child-first, awaited.

        The runtime registries are process-local singletons; they are read lazily so a
        bus built without the runtime (tests, legacy callers) still stops normally.
        Bounded: a stuck child or job must never make the Stop button hang.
        """
        counts = {"children": 0, "jobs": 0}
        try:
            from app.runtime.children import get_child_registry
            reg = get_child_registry()
            if reg is not None:
                counts["children"] = await reg.drain_children(ch.conv_id, reason)
        except Exception as e:  # pragma: no cover - a cascade must never break a stop
            logger.debug(f"[CHATBUS] child cascade failed for conv={ch.conv_id}: {e}")
        try:
            from app.runtime.jobs import get_job_registry
            jobs = get_job_registry()
            if jobs is not None:
                counts["jobs"] = await jobs.drain_owner(ch.conv_id, reason)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[CHATBUS] job cascade failed for conv={ch.conv_id}: {e}")
        if counts["children"] or counts["jobs"]:
            logger.info(f"[CHATBUS] Cascade stop for conv={ch.conv_id}: "
                        f"{counts['children']} child(ren), {counts['jobs']} job(s)")
        return counts

    async def stop(self, conv_id: str, send_frame: bool = True,
                   cascade: bool = True) -> bool:
        """Stop a chat's session (queued or running). Returns True when something was active.

        Two layers (see module docstring): cooperative stop_event + hard-cancel backstop so
        the red Stop button ALWAYS stops the task - even if it is stuck inside a long tool
        execution that does not poll the event on itself.

        cascade=True (2026-12) also stops this chat's delegated children and background
        jobs: pressing Stop means "stop everything this chat started". bus.submit() passes
        cascade=False when a NEW USER MESSAGE supersedes a turn - background work the user
        deliberately started outlives the turn that launched it, which is the whole point
        of a job.

        (2026-11-30 fix) The early-return used to bail out whenever the chat had NO turn
        task, so a Stop pressed while only DELEGATED work was running stopped nothing: the
        children kept burning tokens and the button reported "nothing active". The turn is
        gone but the work the chat started is still alive - that is exactly what Stop must
        stop, so the runtime registries are consulted before deciding there is nothing to do.
        """
        ch = self._channels.get(conv_id)
        if ch is None:
            return False
        live_runtime = self._live_runtime_count(conv_id)
        if ch.task is None and not ch.stop_event.is_set() and live_runtime == 0:
            return False
        if not ch.stop_event.is_set():
            ch.stop_event.set()

        task = ch.task
        if task is not None and not task.done():
            # Hard backstop: only fires when the graceful unwind did NOT finish in time.
            async def _backstop(t=task):
                try:
                    await asyncio.sleep(self._stop_grace_sec)
                except asyncio.CancelledError:
                    return  # bus stopped / app shutting down - nothing to enforce anymore
                if not t.done():
                    logger.warning(f"[CHATBUS] conv={conv_id} did not stop within "
                                   f"{self._stop_grace_sec:.0f}s of the stop request - hard-cancelling task")
                    t.cancel()

            asyncio.create_task(_backstop(), name=f"chat-stop-backstop-{conv_id[:8]}")

        logger.info(f"[CHATBUS] Stop requested for conv={conv_id} "
                    f"(status={ch.status.value}, task_running={bool(ch.task and not ch.task.done())})")
        # A steering message still waiting for an injection point will never run now.
        await self._reject_pending_steers(
            ch,
            "The task was stopped before this steering message could be applied.",
            "task_stopped",
        )
        if cascade:
            # (2026-12) Stop means "stop everything this chat started": children first
            # (a child may itself be holding a job), then the chat's background jobs.
            try:
                await self._cascade_stop(ch, reason="user stop")
            except Exception as e:  # pragma: no cover - a cascade must never break a stop
                logger.debug(f"[CHATBUS] cascade stop failed for conv={conv_id}: {e}")
        if send_frame:
            try:
                await ch.emit_frame({"type": "system",
                                     "content": "Generation stopped. Type new message to continue."})
            except Exception:
                pass
        return True

    # --- stale cleanup (replaces endpoints._cleanup_stale_stop_events) -------------------

    def start_cleanup(self, interval_sec: int = 300) -> None:
        """Drop channels idle too long and prune the legacy stop_events dict alongside."""
        if self._cleanup_task is not None:
            return

        async def _loop():
            while True:
                await asyncio.sleep(interval_sec)
                try:
                    now = time.time()
                    stale = []
                    for cid, ch in list(self._channels.items()):
                        task_idle = ch.task is None or ch.task.done()
                        if not ch.subscribers and task_idle and now - ch.last_activity > interval_sec:
                            stale.append(cid)
                    for cid in stale:
                        del self._channels[cid]
                    # prune the legacy stop_events dict on the same cadence
                    for cid, e in list(self._stop_events.items()):
                        created = e.get("created_at", 0) if isinstance(e, dict) else 0
                        if now - created > interval_sec:
                            self._stop_events.pop(cid, None)
                except Exception as e:  # pragma: no cover - cleanup must never raise
                    logger.debug(f"[CHATBUS] cleanup tick failed: {e}")

        self._cleanup_task = asyncio.create_task(_loop(), name="chat-bus-cleanup")
        logger.info("[CHATBUS] Stale-channel cleanup task started (10 min interval)")

    def stop_cleanup(self) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        self._cleanup_task = None


# ---------------------------------------------------------------------------
# Module-level singleton (created at app startup; tests build their own bus).
# ---------------------------------------------------------------------------

_bus: ChatBus | None = None


def set_chat_bus(bus: ChatBus) -> None:
    global _bus
    _bus = bus


def get_chat_bus() -> ChatBus | None:
    return _bus
