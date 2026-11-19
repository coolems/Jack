"""ChatChannel - one chat conversation's persistent session state.

A ChatChannel outlives any UI WebSocket: a generating chat keeps running inside its
channel, pushing frames live while a socket is attached and buffering them (bounded)
when none is. The final answer is always persisted by the bus.

Fields:
  status        where the session is right now (drives the sidebar dot)
  stop_event    cancellation
  frame buffer  bounded deque for background frames
  subscribers   UI WebSockets attached to this conversation
  steer queue   messages typed WHILE the agent runs, drained at the next safe boundary
"""

import asyncio
import json
from collections import deque
from enum import Enum
import time

import uuid

from client_config import CHAT_BACKGROUND_FRAME_BUFFER, CHAT_STEER_QUEUE_MAX


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
        # The live turn's CycleRecorder, set by run_generation_turn. The
        # question card records through record_runtime() so it lands as a row of THIS turn's
        # block; with no live turn the frame goes out as a plain frame instead.
        self.cycle = None
        # Messages typed by the user DURING a run. Drained by the ReAct loop at its next safe
        # boundary (before a provider call / before pending tool calls), never as a new turn.
        self._steer_queue: deque[dict] = deque(maxlen=max(1, CHAT_STEER_QUEUE_MAX))
        # (2026-11 steer-history fix) ids the ReAct loop already injected. A steer must be
        # applied EXACTLY once: the loop can drain twice (stop / teardown race), so applied
        # ids are remembered and never handed out again by drain_steer().
        self._applied_steers: set[str] = set()
        # Unanswered question cards, keyed by request id, so a
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

        Returns the queued item. The queue is bounded: a flooding client drops the oldest
        unapplied message. Nothing is persisted here - only when the loop applies it."""
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

        Already-applied items are dropped instead of returned, so a steer is never injected
        twice."""
        pending = [item for item in self._steer_queue
                   if item.get("id") not in self._applied_steers]
        self._steer_queue.clear()
        return pending

    # --- applied-steer bookkeeping (2026-11 steer-history fix) -----------------

    def mark_steer_applied(self, steer_id: str | None) -> None:
        """Record that ONE steering message reached the model. Idempotent.

        The loop can drain twice (stop / teardown race), so an applied steer must never be
        handed out again."""
        if not steer_id:
            return
        self._applied_steers.add(str(steer_id))

    def applied_steer_count(self) -> int:
        """How many steering messages this turn actually injected."""
        return len(self._applied_steers)

    def has_unapplied_steer(self) -> bool:
        """True when a queued steer has NOT been injected yet - the gate the ReAct loop uses."""
        return any(item.get("id") not in self._applied_steers for item in self._steer_queue)

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

        Never raises - a dead subscriber is dropped silently. The raw JSON string is buffered
        so replay is byte-identical."""
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
        """Record one runtime entity of THIS chat: an ask_user_question card.

        `entity_kind` is not named `kind`: a payload key called `kind` would collide with the
        argument and raise at call time, so the row would never paint.

        With a live CycleRecorder the frame goes through it - one row of that turn's block,
        persisted in the turn_log payload. With no live turn it is emitted under the turn id it
        was launched with. Never raises."""
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
        id_key = "request_id" if entity_kind == "question" else f"{entity_kind}_id"
        frame = {"type": f"cycle_{entity_kind}", "conv_id": self.conv_id,
                 "turn_id": launch_turn_id or getattr(self, "turn_id", "") or "",
                 id_key: entity_id}
        frame.update(data)
        await self.emit_frame(frame)


class ChannelWebSocket:
    """Provider-facing WebSocket bound to a channel.

    send_text() parses JSON, captures content/thinking chunks into captured_text (partial
    persistence on cancel) and routes every frame through channel.emit_frame() - live push
    or the bounded background buffer. Any other attribute is forwarded to an attached UI
    socket. A dead subscriber never breaks generation.
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
