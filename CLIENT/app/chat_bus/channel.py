"""ChatChannel — one chat conversation's persistent session state (2026-09-08 multi-chat, Phase 3).

A ChatChannel outlives any single UI WebSocket. When a chat is generating and the user
switches to another chat, the generation task keeps running inside its channel: frames are
pushed live while a UI socket is attached, otherwise they are buffered (bounded) so a
re-attaching client can replay the tail. The final answer is always persisted by the bus.

One job per attribute — deliberately small:
  * status        — where this chat's session is right now (drives the sidebar dot)
  * stop_event    — cancellation, owned HERE instead of the handler's local state
  * frame buffer  — bounded deque for background frames (CHAT_BACKGROUND_FRAME_BUFFER)
  * subscribers   — UI WebSockets currently attached to this conversation
      * steer queue   — (2026-10-23) user messages typed WHILE the agent runs, drained by the
                        ReAct loop at its next safe boundary (never a new turn, never a stop)
    """

import asyncio
import json
from collections import deque
from enum import Enum
import time

import uuid

from config import (
    CHAT_BACKGROUND_FRAME_BUFFER,
    CHAT_STEER_QUEUE_MAX,
    NOTICE_QUEUE_MAX,
    NOTICE_MAX_CONSECUTIVE_WAKES,
)


class ChannelStatus(str, Enum):
    """Lifecycle of one chat's background session."""

    IDLE = "idle"
    QUEUED = "queued"          # waiting for a free LLM backend (SERVER queue)
    GENERATING = "generating"  # streaming right now
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class ChatChannel:
    """Persistent per-conversation session state owned by the ChatBus."""

    def __init__(self, conv_id: str):
        self.conv_id = conv_id
        self.last_activity = time.time()   # refreshed on attach/detach/emit (stale cleanup)
        self.status: ChannelStatus = ChannelStatus.IDLE
        self.stop_event = asyncio.Event()
        self.task: "asyncio.Task | None" = None          # running generation task (if any)
        self.queue_position: int = 0                      # >1 while waiting in the SERVER queue
        # (2026-10-23 live steering) True while the CURRENT turn runs the agentic ReAct loop.
        # Steering is only meaningful there (a normal one-shot completion has no next step to
        # inject into). Set by run_generation_turn at turn start.
        self.agent_mode: bool = False
        # (2026-11-08) id of the turn running NOW. A steering message is a fact of that turn,
        # so its frames must carry this id - the UI routes them into the turn's loop-history
        # block (static/ui/cycle-block.js keys blocks by (conv_id, turn_id)). "" when no turn
        # is running or the turn has no cycle history (normal one-shot mode).
        self.turn_id: str = ""
        # (2026-12 runtime) the live turn's CycleRecorder, set by run_generation_turn. The
        # runtime layer (app.runtime.notice_hub, tools/local_tools) records child/job frames
        # through record_runtime() so they land as rows of THIS turn's block; with no live
        # turn (a settlement after the turn ended) they go out as plain frames instead.
        self.cycle = None
        # Messages typed by the user DURING a run. Drained by the ReAct loop at its next safe
        # boundary (before a provider call / before pending tool calls), never as a new turn.
        self._steer_queue: deque[dict] = deque(maxlen=max(1, CHAT_STEER_QUEUE_MAX))
        # (2026-11 steer-history fix) ids the ReAct loop already injected. A steer must be
        # applied EXACTLY once: the loop can drain twice (stop / teardown race), so applied
        # ids are remembered and never handed out again by drain_steer().
        self._applied_steers: set[str] = set()
        # (2026-12 runtime notice lane) the runtime's OWN queue: subagent settlements,
        # background-job settlements, answers to questions the AI asked. Deliberately
        # separate from the steer queue (see queue_notice): a steer is user speech and may
        # defer a premature completion; a notice is runtime information and never does.
        self._notice_queue: deque[dict] = deque(maxlen=max(1, NOTICE_QUEUE_MAX))
        self._applied_notices: set[str] = set()
        # Notices that started a follow-up turn WITHOUT user input. Reset by any real user
        # message; bounded by NOTICE_MAX_CONSECUTIVE_WAKES so the agent's own settlements
        # cannot wake it forever (self-exciting chain).
        self._wake_rounds: int = 0
        # (2026-12 ask_user_question) unanswered question cards, keyed by request id, so a
        # follow-up turn can say "still pending" and a UI reload can re-render the card.
        self._pending_questions: dict[str, dict] = {}
        # (2026-08-23 session stats) duration of the PREVIOUS completed turn, in ACTIVE seconds
        # (approval-menu pauses excluded - see logic.base_mode.SessionTimer). The next turn's
        # config reads this so the UI status bar can show "last session" time. 0 = no previous
        # turn yet on this channel.
        self.last_session_duration_sec: float = 0.0
        self._answer_persisted = False          # (2026-09-09 audit) set by the turn runner after
                                               # the final answer hits the DB -> stale buffer tail may be dropped

        # Frames captured while NO UI socket is attached (user looking at another chat).
        self._buffer: deque[str] = deque(maxlen=max(1, CHAT_BACKGROUND_FRAME_BUFFER))
        self.subscribers: set = set()                     # attached FastAPI WebSockets

    # --- status -----------------------------------------------------------------

    def set_status(self, status: ChannelStatus) -> None:
        if status is not self.status:
            self.status = status

    # --- live steering (2026-10-23) ------------------------------------------------

    def queue_steer(self, text: str, steer_id: str | None = None) -> dict:
        """Queue one mid-run user message for the CURRENT agentic turn.

        Returns the queued item (with the id echoed to the UI). A deque with a bounded
        length is used on purpose: if a client floods the queue the OLDEST unapplied
        message is dropped instead of growing memory forever. The item is NOT persisted
        here - persistence happens only when the loop actually applies it, so a message
        that never reached the model does not pollute the conversation history.
        """
        item = {
            "id": steer_id or uuid.uuid4().hex,
            "text": text,
            "received_at": time.time(),
        }
        self._steer_queue.append(item)
        self.last_activity = time.time()
        return item

    def has_pending_steer(self) -> bool:
        """True when at least one steering message is waiting to be injected."""
        return bool(self._steer_queue)

    def pending_steer_count(self) -> int:
        return len(self._steer_queue)

    def drain_steer(self) -> list[dict]:
        """Remove and return every steering message the loop has NOT injected yet (FIFO).

        Already-applied items are dropped from the queue instead of returned: they were
        injected once already, so handing them out again would duplicate the user turn.
        """
        pending = [item for item in self._steer_queue
                   if item.get("id") not in self._applied_steers]
        self._steer_queue.clear()
        return pending

    # --- applied-steer bookkeeping (2026-11 steer-history fix) -----------------

    def mark_steer_applied(self, steer_id: str | None) -> None:
        """Record that ONE steering message reached the model. Idempotent.

        An applied steer must never be drained again: the loop can drain twice (stop /
        teardown race), and re-injecting the same text would duplicate the user turn and
        the history row. drain_steer() therefore drops anything already marked applied.
        """
        if not steer_id:
            return
        self._applied_steers.add(str(steer_id))

    def applied_steer_count(self) -> int:
        """How many steering messages this turn actually injected."""
        return len(self._applied_steers)

    def has_unapplied_steer(self) -> bool:
        """True when a queued steer has NOT been injected yet.

        This is the gate the ReAct loop uses: has_pending_steer() alone would keep
        firing for a steer that was already applied but not yet removed from the queue.
        """
        return any(item.get("id") not in self._applied_steers for item in self._steer_queue)

    # --- runtime notices (2026-12 subagents / background jobs / questions) ----------
    # The runtime's own lane, deliberately SEPARATE from the steer queue:
    #   * a steer is USER speech and may defer a premature completion (interjection);
    #   * a notice is runtime information (a child settled, a job settled, a question
    #     answered) and must never defer completion.
    # Same injection boundaries, different header + different persistence flag
    # (messages.is_notice) + different turn_log row kind. Ids are deduped the same way:
    # one settlement delivers exactly one notice, even if the loop drains twice.
    #
    # _wake_rounds counts notices that started a FOLLOW-UP turn with no user input. Any
    # real user message resets it (bus.submit / steer), which is what keeps an idle chat
    # from being woken forever by its own children (self-exciting chain).
    def queue_notice(self, text: str, source: str = "runtime",
                     notice_id: str | None = None, meta: dict | None = None) -> dict:
        """Queue one runtime notice for this chat's live turn (or for a later wake).

        Bounded like the steer queue: a flood of settlements drops the OLDEST unapplied
        notice rather than growing memory. Nothing is persisted here - persistence happens
        when the loop actually injects it (or when a wake turn runs it), so a notice that
        never reached the model never pollutes history.
        """
        item = {
            "id": notice_id or uuid.uuid4().hex,
            "source": str(source or "runtime"),
            "text": text or "",
            "meta": meta or {},
            "received_at": time.time(),
        }
        self._notice_queue.append(item)
        self.last_activity = time.time()
        return item

    def has_unapplied_notice(self) -> bool:
        """True when a notice is waiting that has NOT been injected yet."""
        return any(item.get("id") not in self._applied_notices for item in self._notice_queue)

    def pending_notice_count(self) -> int:
        return len(self._notice_queue)

    def drain_notice(self) -> list[dict]:
        """Remove and return every notice the loop has not injected yet (FIFO).

        Already-applied ids are dropped instead of returned: a second injection of one
        settlement would make the model believe a child finished twice.
        """
        pending = [item for item in self._notice_queue
                   if item.get("id") not in self._applied_notices]
        self._notice_queue.clear()
        return pending

    def mark_notice_applied(self, notice_id: str | None) -> None:
        """Record that ONE notice reached the model. Idempotent, never re-handed out."""
        if not notice_id:
            return
        self._applied_notices.add(str(notice_id))

    def applied_notice_count(self) -> int:
        return len(self._applied_notices)

    def notice_wake_allowed(self) -> bool:
        """True when an idle chat may be woken by a notice (consecutive-wake budget).

        The budget exists because notices are generated by the agent's own work: without
        a cap, one child settling can wake the parent, which delegates again, forever.
        reset_notice_wakes() is called by every real user input, so the user always
        re-opens the lane.
        """
        return self._wake_rounds < max(0, NOTICE_MAX_CONSECUTIVE_WAKES)

    def bump_notice_wakes(self) -> int:
        """Count one notice-driven follow-up turn. Returns the new count."""
        self._wake_rounds += 1
        return self._wake_rounds

    def reset_notice_wakes(self) -> None:
        """A real user message (or a new user turn) re-opens the notice lane."""
        self._wake_rounds = 0

    # --- pending questions (ask_user_question, timed mode) -------------------------
    # A question card the AI asked that is still unanswered. Lives on the channel so a
    # follow-up turn can tell the model "still pending, keep working" and so the UI can
    # re-render the card on a reload. Answers arrive through the question endpoint.
    def add_pending_question(self, request_id: str, payload: dict) -> dict:
        item = {"id": request_id, "payload": payload or {}, "asked_at": time.time()}
        self._pending_questions[request_id] = item
        self.last_activity = time.time()
        return item

    def pending_questions(self) -> list[dict]:
        return list(self._pending_questions.values())

    def has_pending_question(self, request_id: str | None = None) -> bool:
        if request_id is None:
            return bool(self._pending_questions)
        return request_id in self._pending_questions

    def drop_pending_question(self, request_id: str | None) -> None:
        if request_id:
            self._pending_questions.pop(request_id, None)

    # --- subscribers (UI sockets attached to this chat) ---------------------------

    def attach(self, ws) -> None:
        """A UI socket started listening on this conversation."""
        self.subscribers.add(ws)
        self.last_activity = time.time()

    def detach(self, ws) -> bool:
        """A UI socket stopped listening. Returns True when NO subscriber is left."""
        self.subscribers.discard(ws)
        self.last_activity = time.time()
        return not self.subscribers

    # --- frame routing -------------------------------------------------------------

    async def emit_frame(self, frame: dict) -> None:
        """Route one provider frame to every attached UI socket; buffer if nobody listens.

        Never raises — a dead subscriber is dropped silently (the bus re-attaches on
        reconnect). The raw JSON string is what gets buffered so replay is byte-identical.
        """
        text = json.dumps(frame)
        self.last_activity = time.time()
        live = [ws for ws in list(self.subscribers)]
        if not live:
            self._buffer.append(text)
            return
        for ws in live:
            try:
                await ws.send_text(text)
            except Exception:
                # dead subscriber — drop it; the channel keeps working for the rest
                self.subscribers.discard(ws)

    def buffered_frames(self, limit: int = 0) -> list[str]:
        """Tail of the background buffer (limit=0 → all). For re-attach replay."""
        items = list(self._buffer)
        return items[-limit:] if limit > 0 else items

    def clear_buffer(self) -> None:
        self._buffer.clear()

    async def emit_raw(self, text: str) -> None:
        """Route a pre-serialized frame (non-JSON or opaque payloads)."""
        live = [ws for ws in list(self.subscribers)]
        if not live:
            self._buffer.append(text)
            return
        for ws in live:
            try:
                await ws.send_text(text)
            except Exception:
                self.subscribers.discard(ws)

    async def record_runtime(self, entity_kind: str, entity_id: str, **data) -> None:
        """Record one runtime entity (2026-12) of THIS chat: a child, a job, a question.

        (2026-12 fix) The first parameter was named `kind`, which collided with the `kind=`
        payload key the job callers pass (a job's producer kind: 'python' / 'bash'). Python
        raises "got multiple values for argument 'kind'" at CALL time, before the body runs,
        so the TypeError escaped this method and the caller's best-effort except swallowed
        it: NO job row ever painted, on start or on settlement. Renamed to entity_kind; the
        payload key stays `kind`, which is what CycleRecorder.job() and the UI read.

        When a live turn owns a CycleRecorder, the frame goes through it, so the entity is
        ONE row of that turn's block and it is persisted in the turn_log payload (a reload
        shows the same rows the live turn showed). With no live turn — a child or job that
        settles AFTER the turn ended, which is the normal case — the frame is emitted
        directly with the turn id it was launched under, so the UI can still settle that
        block's row. Never raises: history is best-effort and must not break a settlement.
        """
        cycle = getattr(self, "cycle", None)
        recorder = getattr(cycle, entity_kind, None)
        # turn_id is frame plumbing, not an entity field: the recorder already stamps its
        # own turn id. Keep it for the fallback frame so a settlement that arrives AFTER the
        # turn ended still lands in the block that launched it.
        launch_turn_id = data.pop("turn_id", None)
        if recorder is not None:
            try:
                await recorder(entity_id, **data)
                return
            except Exception:
                pass  # fall through to the plain frame so the UI still gets the fact
        frame = {"type": f"cycle_{entity_kind}", "conv_id": self.conv_id,
                 "turn_id": launch_turn_id or getattr(self, "turn_id", "") or "",
                 f"{entity_kind}_id" if entity_kind != "question" else "request_id": entity_id}
        frame.update(data)
        await self.emit_frame(frame)


class ChannelWebSocket:
    """Provider-facing WebSocket bound to a channel (2026-09-08 multi-chat).

    Replaces the per-connection _ContentCapturingWebSocket for background sessions.
    The AI provider streams through this object exactly like it did through a UI socket:

      * send_text(data) - parses JSON; content/thinking chunks are captured into
        captured_text (so partial persistence still works on cancel) AND every frame is
        routed through channel.emit_frame() -> live push to attached UI sockets, or the
        bounded background buffer when nobody is looking at this chat.
      * any other attribute - forwarded to an attached UI socket when one exists
        (defensive pass-through, same contract as the old capturing proxy).

    A dead subscriber never breaks generation: emit_frame() drops it silently and the
    channel keeps buffering for whoever re-attaches next.
    """

    def __init__(self, channel):
        self._channel = channel
        self.captured_text = ""

    def reset_capture(self) -> None:
        """Fresh capture buffer (called before each generation turn)."""
        self.captured_text = ""

    async def send_text(self, data):
        try:
            msg = json.loads(data)
        except (ValueError, TypeError):
            msg = None
        if isinstance(msg, dict):
            t = msg.get("type")
            chunk = msg.get("content", "")
            if t in ("content", "thinking") and isinstance(chunk, str) and chunk:
                self.captured_text += chunk
            await self._channel.emit_frame(msg)
        else:
            # non-JSON frame - route the raw text (buffered when no UI socket is attached)
            await self._channel.emit_raw(data if isinstance(data, str) else json.dumps(data))

    def __getattr__(self, name):
        # forward anything else to an attached UI socket when one exists; otherwise a
        # safe no-op so background generation can never crash on a missing subscriber.
        subs = self._channel.subscribers
        if subs:
            return getattr(next(iter(subs)), name)

        async def _noop(*args, **kwargs):
            return None

        return _noop
