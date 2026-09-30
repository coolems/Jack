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
"""

import asyncio
import json
from collections import deque
from enum import Enum
import time

from config import CHAT_BACKGROUND_FRAME_BUFFER


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
        self._answer_persisted = False          # (2026-09-09 audit) set by the turn runner after
                                               # the final answer hits the DB -> stale buffer tail may be dropped

        # Frames captured while NO UI socket is attached (user looking at another chat).
        self._buffer: deque[str] = deque(maxlen=max(1, CHAT_BACKGROUND_FRAME_BUFFER))
        self.subscribers: set = set()                     # attached FastAPI WebSockets

    # --- status -----------------------------------------------------------------

    def set_status(self, status: ChannelStatus) -> None:
        if status is not self.status:
            self.status = status

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
