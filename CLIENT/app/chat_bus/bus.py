"""ChatBus — registry of per-chat background sessions (2026-09-08 multi-chat, Phase 3).

One ChatChannel per conversation id; each channel owns its generation task, stop event and
frame buffer. The UI WebSocket handler becomes a thin subscriber: it attaches/detaches its
socket to the channel instead of owning the whole turn inline. That is what lets several
chats generate at once — switching chats in the browser no longer kills anything.

Responsibilities (and ONLY these):
  * get_or_create / submit   — create or reuse a channel, spawn the background task
  * attach / detach          — UI socket lifecycle per conversation
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

from .channel import ChatChannel, ChannelStatus, ChannelWebSocket
from .generation_task import run_generation_turn, register_stop_event, unregister_stop_event

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
                if ch.task is not None and not ch.task.done()]

        async def _stop_one(ch: ChatChannel) -> bool:
            try:
                await ch.emit_frame({"type": "system",
                                     "content": f"Generation stopped ({reason}). Type new message to continue."})
            except Exception:
                pass
            if not ch.stop_event.is_set():
                ch.stop_event.set()
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

    async def submit(self, ws, conv_id: str, prepared: dict) -> ChatChannel:
        """Start a background generation turn for *prepared* (see handler._prepare_user_message).

        If the chat already has a live session the old one is stopped first — a new user
        message supersedes it (same as clicking send while generating before this refactor,
        which used to be impossible because the receive loop was busy).
        """
        self._bind_owner_loop()
        ch = self.get_or_create(conv_id)

        if ch.task is not None and not ch.task.done():
            logger.info(f"[CHATBUS] New message supersedes live session for conv={conv_id} — stopping it")
            await self.stop(conv_id, send_frame=False)

        channel_ws = ChannelWebSocket(ch)
        register_stop_event(self._stop_events, conv_id, ch.stop_event)
        ch.set_status(ChannelStatus.GENERATING)  # may be corrected to QUEUED by queue_status frames
        prepared = dict(prepared)
        prepared["history"] = list(prepared.get("history", []))

        task = asyncio.create_task(
            self._guarded_turn(ch, channel_ws, prepared),
            name=f"chat-turn-{conv_id[:8]}",
        )
        ch.task = task
        return ch

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

    async def stop(self, conv_id: str, send_frame: bool = True) -> bool:
        """Stop a chat's session (queued or running). Returns True when something was active.

        Two layers (see module docstring): cooperative stop_event + hard-cancel backstop so
        the red Stop button ALWAYS stops the task - even if it is stuck inside a long tool
        execution that does not poll the event on its own.
        """
        ch = self._channels.get(conv_id)
        if ch is None or (ch.task is None and not ch.stop_event.is_set()):
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
