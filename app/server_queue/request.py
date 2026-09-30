"""ChatRequest — one queued chat request travelling from a client to an LLM backend.

2026-09-08 multi-chat (Phase 2). A ChatRequest is the unit of work for the server-side
queue/worker pool: every "request" frame arriving over a direct WS or through the web
relay becomes exactly one ChatRequest, lands in :class:`~app.server_queue.queue_manager.RequestQueue`,
and is later executed by a worker on whichever llama backend is free.

The request owns its stop event (created owner-scoped by the WS layer) so cancellation
works identically while the request is still QUEUED or already RUNNING — both paths just
set the same asyncio.Event that the streaming layer polls.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ChatRequest:
    """One chat turn submitted by one client conversation.

    Attributes:
        req_id: unique id (uuid4 hex) — used in logs and stats.
        conv_id: the CLIENT-side conversation id (may be "" for anonymous turns).
        owner_key: who owns this request; cancellation is always scoped to an owner:
            direct path  -> the connection id (``conn_id``),
            relay path   -> ``f"relay:{client_id}"``.
        sink: transport-agnostic frame sink that delivers response frames back to the
            requesting chat channel (see :mod:`app.server_queue.sink`).
        model / messages / temperature / enable_thinking / tools: generation parameters,
            already profile-filtered by the WS layer before enqueueing.
        stop_event: shared cancellation event (owner-scoped); set = abort.
        created_at: monotonic timestamp for queue-age stats.
    """

    owner_key: str
    sink: Any  # FrameSink (kept untyped to avoid a circular import at module load)
    model: str = ""
    messages: list = field(default_factory=list)
    temperature: float = 0.7
    enable_thinking: bool = False
    conv_id: str = ""
    tools: Optional[list] = None
    stop_event: Any = None  # asyncio.Event (None tolerated for legacy direct calls)
    req_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.monotonic)

    def is_cancelled(self) -> bool:
        """True when the owner stopped this request (works queued AND running)."""
        return self.stop_event is not None and self.stop_event.is_set()

    @property
    def age_sec(self) -> float:
        """Seconds spent in queue + running so far."""
        return time.monotonic() - self.created_at
