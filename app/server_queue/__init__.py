"""app.server_queue — SERVER-side chat request queue + worker pool (2026-09-08 multi-chat, Phase 2).

Every chat request from any client lands in a FIFO :class:`RequestQueue`; one worker per
llama backend pulls the next request as soon as its instance is free. With N configured
backends up to N chats generate simultaneously; with one backend chats are served strictly
one after another — exactly the requested behaviour.

Public API (kept minimal on purpose):
    ChatRequest     — unit of work (request.py)
    RequestQueue    — bounded FIFO + owner-scoped cancellation + stats (queue_manager.py)
    WorkerPool      — N workers ↔ N backends, auto-dispatch (worker_pool.py)
    FrameSink & co  — transport-agnostic frame delivery adapters (sink.py)

Singletons: set_queue()/get_queue(), set_worker_pool()/get_worker_pool() — wired at boot
by CoolemsServerProvider; unit tests construct their own instances.
"""

from .queue_manager import RequestQueue, get_queue, set_queue
from .request import ChatRequest
from .sink import DirectWsSink, FrameSink, RelayTargetSink
from .worker_pool import WorkerPool, get_worker_pool, set_worker_pool

__all__ = [
    "ChatRequest",
    "DirectWsSink",
    "FrameSink",
    "RelayTargetSink",
    "RequestQueue",
    "WorkerPool",
    "get_queue",
    "set_queue",
    "get_worker_pool",
    "set_worker_pool",
]
