"""RequestQueue — the FIFO backlog of ChatRequests waiting for a free LLM backend.

2026-09-08 multi-chat (Phase 2). Every chat request from ANY chat lands here; workers
pull one at a time, so with N llama backends up to N chats generate simultaneously and
the rest wait fairly in FIFO order. With a single backend this degrades exactly to the
old behaviour: strictly one request at a time, others queued.

2026-09-08 audit redesign (owner-indexed storage). The backlog is kept as an owner-keyed
dict of deques PLUS a global FIFO index, guarded by ONE asyncio.Condition:

  * enqueue / get        — bounded FIFO; over-limit enqueues are rejected with a reason
  * cancel_owner         — O(k) in-place removal of one owner's entries (k = that
                           owner's queued requests). There is NO drain-and-rebuild phase,
                           so an interleaved enqueue can never collide with a rebuild and
                           no other owner's request can ever be silently dropped.
  * stats + periodic log — observability for operators and the queue_status frames

Atomicity model (why this is safe by construction): every mutation acquires the condition
and performs its work with ZERO await points inside that section, so on a single event
loop each mutation runs to completion without yielding — no other coroutine can observe or
interleave half-finished state. Cancellation removes entries in place from BOTH structures;
there is simply no rebuild window for an enqueue to race against.

The public API matches the first Phase-2 cut (enqueue / get / cancel_owner / mark_running /
mark_done / stats / drain_and_cancel_all / *_stats_logger), so callers, sinks and the worker
pool are untouched by this redesign.
"""

import asyncio
import logging
from collections import deque
from typing import Optional, Tuple

from config import QUEUE_MAX_PENDING, QUEUE_STATS_LOG_INTERVAL_SEC
from .request import ChatRequest

logger = logging.getLogger("COOLEMS.Provider.Queue")


class _LegacyQueueView:
    """Read-only stand-in for the old asyncio.Queue attribute (test/tooling compatibility)."""

    def __init__(self, queue: "RequestQueue"):
        self._q = queue

    def empty(self) -> bool:
        return self._q.pending_count() == 0


class RequestQueue:
    """Bounded FIFO of pending chat requests with owner-scoped cancellation.

    Storage model (audit redesign):
      * ``_by_owner[owner_key]`` — deque of that owner's queued requests (FIFO within owner)
      * ``_fifo``                — global order across ALL owners; workers pop from its head
      * ``_cond``                — single asyncio.Condition guarding BOTH structures

    Every mutation acquires the condition and does its work with no await points inside, so
    it is atomic on a single event loop by construction. Cancellation removes entries in
    place from both structures — there is no rebuild phase that could race an enqueue.
    """

    def __init__(self, max_pending: int = QUEUE_MAX_PENDING):
        self._max_pending = max(1, max_pending)
        self._by_owner: dict[str, deque] = {}
        self._fifo: list[ChatRequest] = []          # global FIFO index (head = next served)
        self._cond = asyncio.Condition()            # guards _fifo + _by_owner and wakes workers
        # running requests by owner_key (one chat turn per owner at a time on the server side)
        self._running: dict[str, ChatRequest] = {}
        self._stats = {"total_enqueued": 0, "total_rejected": 0, "total_served": 0}
        self._log_task: Optional[asyncio.Task] = None

    # --- compatibility view ------------------------------------------------------

    @property
    def _queue(self) -> _LegacyQueueView:
        """Read-only shim so pre-audit tests/tools that inspect ``q._queue`` keep working."""
        return _LegacyQueueView(self)

    # --- core ---------------------------------------------------------------------

    @property
    def max_pending(self) -> int:
        return self._max_pending

    def pending_count(self) -> int:
        """Number of requests waiting right now (not counting the running one per worker).

        A direct read is safe without the lock: every mutation runs to completion with no
        await points inside its critical section, so at any instant this method can run,
        no coroutine is mid-mutation — we always see a consistent pre- or post-state.
        """
        return len(self._fifo)

    async def enqueue(self, req: ChatRequest) -> Tuple[bool, str]:
        """Add *req* to the backlog. Returns (accepted, reason).

        Rejection happens only when the backlog is full — the caller sends a clear error
        frame to the client instead of silently dropping work. The append runs under the
        condition with no await points inside: an interleaved cancel_owner either sees the
        request or does not, never half of it.
        """
        if req.is_cancelled():
            return False, "already cancelled"

        async with self._cond:
            if len(self._fifo) >= self._max_pending:
                self._stats["total_rejected"] += 1
                reason = (f"server queue is full ({self._max_pending} pending) - "
                          f"try again in a moment")
                logger.warning("[QUEUE] Rejected request %s (conv=%s): %s",
                               req.req_id, req.conv_id or "?", reason)
                return False, reason

            position = len(self._fifo) + 1
            self._fifo.append(req)
            self._by_owner.setdefault(req.owner_key, deque()).append(req)
            self._stats["total_enqueued"] += 1
            if position > 1:
                logger.info("[QUEUE] Request %s (conv=%s owner=%s) queued at position %d",
                            req.req_id, req.conv_id or "?", req.owner_key, position)

            # Wake a waiting worker while still holding the lock (Condition requires it).
            self._cond.notify_all()
        return True, f"position {position}"

    async def get(self) -> ChatRequest:
        """Workers await this; blocks until a request is available.

        Pops the head of the global FIFO and removes it from its owner's deque in one atomic
        section. (cancel_owner normally removes cancelled entries itself, so a worker only
        ever pops live work.)
        """
        async with self._cond:
            while not self._fifo:
                await self._cond.wait()
            req = self._fifo.pop(0)
            dq = self._by_owner.get(req.owner_key)
            if dq and dq[0] is req:
                dq.popleft()
            elif dq and req in dq:  # pragma: no cover - defensive (FIFO order prevents this)
                dq.remove(req)
            return req

    # --- lifecycle bookkeeping (called by the worker pool) -------------------------

    def mark_running(self, req: ChatRequest) -> None:
        self._running[req.owner_key] = req

    def mark_done(self, req: ChatRequest) -> None:
        self._running.pop(req.owner_key, None)
        if not req.is_cancelled():
            self._stats["total_served"] += 1

    # --- cancellation ---------------------------------------------------------------

    async def cancel_owner(self, owner_key: str) -> int:
        """Abort everything owned by *owner_key*: queued entries are REMOVED from both
        structures in place (no drain/rebuild), and the running one (if any) gets its stop
        event set. Returns how many requests were affected.

        The removal runs under the condition with zero await points, so it is atomic by
        construction: an interleaved enqueue can never collide with a rebuild because there
        IS no rebuild — entries are simply taken out of both structures where they sit.
        """
        queued_removed = 0
        async with self._cond:
            dq = self._by_owner.get(owner_key)
            if dq is not None:
                for item in list(dq):
                    if item.is_cancelled():
                        continue
                    if item.stop_event is not None:
                        item.stop_event.set()
                    self._fifo.remove(item)          # O(n), n <= max_pending (small, bounded)
                    queued_removed += 1
                dq.clear()
                del self._by_owner[owner_key]

        affected = queued_removed
        running = self._running.get(owner_key)
        if running is not None and not running.is_cancelled():
            if running.stop_event is not None:
                running.stop_event.set()
            affected += 1
        if affected:
            logger.info("[QUEUE] Cancelled %d request(s) for owner=%s", affected, owner_key)
        return affected

    # --- observability -----------------------------------------------------------------

    def drain_and_cancel_all(self) -> int:
        """Remove every queued request and set their stop events (shutdown path).

        Returns how many requests were aborted. Running requests are the worker pool's
        responsibility (they observe the same stop events via owner cancellation).

        A direct mutation without taking the lock is safe for the same reason as
        pending_count: this method has no await points, so it runs to completion inside one
        event-loop tick and no other coroutine can be mid-mutation at that instant.
        """
        aborted = len(self._fifo)
        for item in self._fifo:
            if item.stop_event is not None and not item.stop_event.is_set():
                item.stop_event.set()
        self._fifo.clear()
        self._by_owner.clear()
        return aborted

    def stats(self) -> dict:
        return {
            "pending": self.pending_count(),
            "running": len(self._running),
            "max_pending": self._max_pending,
            **self._stats,
        }

    def start_stats_logger(self, interval_sec: int = QUEUE_STATS_LOG_INTERVAL_SEC) -> None:
        """Periodic queue-depth heartbeat - INFO only when state CHANGES (2026-09-10 log-noise fix).

        The loop wakes every *interval_sec* seconds and compares the snapshot
        (pending, running, served, rejected) with what was last logged:
          * first tick  -> INFO baseline line (proves the heartbeat is alive after boot),
          * later ticks -> INFO when something changed, DEBUG otherwise (silent by default).

        The old unconditional-INFO-every-tick behaviour flooded the log with identical lines
        on an idle server. interval <= 0 still disables the logger entirely; start/stop stay
        idempotent as before.
        """
        if interval_sec and interval_sec > 0 and self._log_task is None:
            last_logged = [None]

            def _snapshot() -> tuple:
                s = self.stats()
                return (s["pending"], s["running"], s["total_served"], s["total_rejected"])

            async def _loop():
                while True:
                    await asyncio.sleep(interval_sec)
                    snap = _snapshot()
                    if last_logged[0] is None or snap != last_logged[0]:
                        level, last_logged[0] = logging.INFO, snap
                    else:
                        level = logging.DEBUG
                    logger.log(level, "[QUEUE] stats pending=%d running=%d served=%d rejected=%d", *snap)

            self._log_task = asyncio.create_task(_loop(), name="queue-stats-logger")

    def stop_stats_logger(self) -> None:
        if self._log_task is not None and not self._log_task.done():
            self._log_task.cancel()
        self._log_task = None


# ---------------------------------------------------------------------------
# Module-level singleton (created at boot by the coolems server provider;
# None until then — direct unit tests build their own instances).
# ---------------------------------------------------------------------------

_queue: Optional[RequestQueue] = None


def set_queue(queue: RequestQueue) -> None:
    global _queue
    _queue = queue


def get_queue() -> Optional[RequestQueue]:
    return _queue
