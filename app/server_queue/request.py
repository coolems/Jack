"""ChatRequest — one queued chat request travelling from a client to an LLM backend.

2026-09-08 multi-chat (Phase 2); 2026-10 fleet: a request now carries its MODEL GROUP
and its USER identity, because the queue is per model and per-user slot caps are
enforced while picking the next request.

A ChatRequest is the unit of work for the server-side queue/worker pool: every
"request" frame arriving over a direct WS or through the web relay becomes exactly one
ChatRequest, lands in the queue of its model group, and is later executed by a worker
on one of that group's slots.

The request owns its stop event (created owner-scoped by the WS layer) so cancellation
works identically while the request is still QUEUED or already RUNNING — both paths just
set the same asyncio.Event that the streaming layer polls.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.providers.llama.model_identity import model_key as model_key_of


@dataclass
class ChatRequest:
    """One chat turn submitted by one client conversation.

    Attributes:
        req_id: unique id (uuid4 hex) — used in logs and stats.
        conv_id: the CLIENT-side conversation id (may be "" for anonymous turns).
        owner_key: who owns this request; cancellation is always scoped to an owner:
            direct path  -> the connection id (``conn_id``),
            relay path   -> ``f"relay:{client_id}"``.
        user_key: stable USER identity (email), shared by all of that user's
            connections/conversations. Per-user slot caps count against this.
        max_parallel: how many generations this user may run at once (profile
            ``max_parallel_slots``). <= 0 means unlimited (internal/test use only).
        model_key: canonical model group (basename, lowercase, no .gguf). Derived from
            ``model`` when not supplied; the queue is keyed by it.
        context_window: per-slot context of the backend that runs this request
            (filled at dispatch; 0 = use CONTEXT_WINDOW_TOKENS).
        backend: label of the node that ran it (error provenance / logs).
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

    # --- 2026-10 fleet routing ---------------------------------------------------
    model_key: str = ""
    user_key: str = ""
    max_parallel: int = 0
    context_window: int = 0
    backend: str = ""

    def __post_init__(self) -> None:
        if not self.model_key:
            self.model_key = model_key_of(self.model)

    def is_cancelled(self) -> bool:
        """True when the owner stopped this request (works queued AND running)."""
        return self.stop_event is not None and self.stop_event.is_set()

    @property
    def age_sec(self) -> float:
        """Seconds spent in queue + running so far."""
        return time.monotonic() - self.created_at
