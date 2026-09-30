"""WorkerPool — N background workers, one per llama backend, pulling from RequestQueue.

2026-09-08 multi-chat (Phase 2). This is where "first free server picks a request" happens:
each worker owns exactly ONE :class:`~app.providers.llama.backend_registry.LlmBackend` and
loops forever — take the next queued ChatRequest, run it on its backend's provider through
the shared chat core (:func:`app.providers.coolems.chat_relay.run_chat_core`), deliver the
final frames through the request's sink, free the slot. The next queued request is picked up
automatically by whichever worker becomes free first.

With one configured llama server this pool has exactly one worker: chats are served strictly
one after another (the requested single-server behaviour). With N servers, up to N chats
generate simultaneously and the rest wait fairly in FIFO order.

Cancellation model: a ChatRequest carries its owner-scoped stop event. A request cancelled
while QUEUED is skipped by the worker that pops it (a "cancelled" frame goes out); one
cancelled while RUNNING aborts llama generation via the same event passed to chat_stream.
"""

import asyncio
import logging
from typing import Optional

from .queue_manager import RequestQueue
from .request import ChatRequest

# Bound at IMPORT time on purpose: the chat core is stable once this module loads, and
# tests patch worker_pool.run_chat_core (module attribute) instead of re-importing.
from app.providers.coolems.chat_relay import run_chat_core

logger = logging.getLogger("COOLEMS.Provider.Queue.Workers")


class WorkerPool:
    """One worker coroutine per backend; workers are the only consumers of the queue."""

    def __init__(self, registry, queue: RequestQueue):
        self._registry = registry
        self._queue = queue
        self._tasks: list[asyncio.Task] = []
        self._started = False

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Spawn one worker per configured backend (idempotent)."""
        if self._started:
            return
        backends = list(self._registry.backends.values())
        for b in backends:
            task = asyncio.create_task(
                self._worker_loop(b), name=f"llm-worker-{b.instance.name}"
            )
            self._tasks.append(task)
        self._started = True
        logger.info("[WORKERS] Started %d worker(s): %s",
                    len(self._tasks), ", ".join(b.label for b in backends))

    async def stop(self, timeout: float = 5.0) -> None:
        """Cancel all workers and wait briefly for them to wind down."""
        if not self._started:
            return
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True), timeout=timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning("[WORKERS] Worker shutdown issue: %s", e)
        self._tasks = []
        self._started = False
        logger.info("[WORKERS] All workers stopped")

    # --- worker loop -----------------------------------------------------------

    async def _worker_loop(self, backend) -> None:
        """Take requests forever; each one runs on THIS backend's provider."""
        while True:
            req: ChatRequest = await self._queue.get()
            try:
                await self._execute(backend, req)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - defensive: worker must never die
                logger.error("[WORKERS] Worker %s crashed on request %s: %s",
                             backend.label, req.req_id, e, exc_info=True)

    async def _execute(self, backend, req: ChatRequest) -> None:
        """Run one queued request end-to-end (frames flow through req.sink)."""
        # Cancelled while still in the queue — skip without touching the backend.
        if req.is_cancelled():
            logger.info("[WORKERS] Skipping cancelled request %s (conv=%s)", req.req_id, req.conv_id or "?")
            await self._send(req.sink, {"type": "cancelled", "req_id": req.req_id})
            return

        # Backend became unhealthy between queueing and pickup: fail fast with a clear frame.
        if not backend.healthy:
            logger.warning("[WORKERS] Request %s (conv=%s) dropped — backend %s is unhealthy",
                           req.req_id, req.conv_id or "?", backend.label)
            await self._send(req.sink, {
                "type": "error",
                "message": f"LLM backend {backend.label} is unavailable - please retry.",
            })
            return

        # Model-switch drain policy: a switch is reloading the backend(s). Running requests
        # were already admitted and finish; NEW ones get a clear, retryable error instead of
        # being served by a half-reloaded model.
        if getattr(self._registry, "switch_in_progress", False):
            logger.info("[WORKERS] Request %s (conv=%s) rejected - model switch in progress",
                        req.req_id, req.conv_id or "?")
            await self._send(req.sink, {
                "type": "error",
                "message": "model switch in progress - please retry in a moment",
            })
            return

        # Tell the chat its turn started (UI can drop the "queued" badge).
        if req.conv_id:
            await self._send(req.sink, {"type": "queue_start", "conv_id": req.conv_id})

        self._queue.mark_running(req)
        try:
            delivered = await run_chat_core(
                None,  # server_provider unused by the core except for logging context
                backend.provider(), req.sink,
                model=req.model, messages=req.messages, temperature=req.temperature,
                enable_thinking=req.enable_thinking, conv_id=req.conv_id, tools=req.tools,
                stop_event=req.stop_event,
            )
            if not delivered and req.is_cancelled():
                await self._send(req.sink, {"type": "cancelled", "req_id": req.req_id})
        finally:
            self._queue.mark_done(req)

    @staticmethod
    async def _send(sink, frame: dict) -> None:
        """Best-effort frame delivery (sink already swallows closed-socket errors)."""
        try:
            await sink.push_frame(frame)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("[WORKERS] status frame not delivered: %s", e)


# ---------------------------------------------------------------------------
# Module-level singleton (created at boot by the coolems server provider;
# None until then — unit tests build their own pools).
# ---------------------------------------------------------------------------

_pool: Optional[WorkerPool] = None


def set_worker_pool(pool: WorkerPool) -> None:
    global _pool
    _pool = pool


def get_worker_pool() -> Optional[WorkerPool]:
    return _pool
