"""ChatRequest - one queued chat request travelling from a client to an LLM backend.

Carries its model group and user identity: the queue is per model, and per-user slot caps
are enforced when the next request is picked. Every "request" frame over a direct WS or
the web relay becomes exactly one ChatRequest, lands in its model group's queue and is
executed by a worker on one of that group's slots.

The request owns its stop event (created owner-scoped by the WS layer), so cancellation
works the same whether it is still queued or already running: both paths set the
asyncio.Event the streaming layer polls.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.providers.llama.model_identity import model_key as model_key_of


@dataclass
class ChatRequest:
    """One chat turn submitted by one client conversation."""

    owner_key: str
    sink: Any  # FrameSink (kept untyped to avoid a circular import at module load)
    model: str = ""
    messages: list = field(default_factory=list)
    temperature: float = 0.7
    # Thinking: bool (show reasoning) or canonical level string
    # "none"/"low"/"medium"/"high" chosen from the serving model's capabilities. Kept
    # verbatim to the provider, which translates it via ThinkingPolicy.
    enable_thinking: Any = False
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
