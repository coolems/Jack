"""RequestQueue — per-MODEL FIFO backlogs of ChatRequests waiting for a free slot.

2026-09-08 multi-chat (Phase 2); 2026-10 fleet: the fleet can serve several models, so
the backlog is partitioned by model group. A Qwen request can never sit in front of a
Gemma slot, and a saturated Qwen queue never rejects a Gemma request. Within a group the
order is FIFO, and per-user slot caps are enforced when a worker picks the next request.

Storage model:

  * ``_by_model[key]``    — FIFO list of that model's queued requests (workers pop here)
  * ``_by_owner[owner]``  — that owner's queued requests (in-place cancellation)
  * ``_running[owner]``   — the request currently running for that owner
  * ``_running_by_user``  — how many generations each USER is running right now
  * ``_running_by_model`` — how many slots of each model group are busy (stats)
  * ``_cond``             — ONE asyncio.Condition guarding all of the above

Atomicity: every mutation acquires the condition and does its work with ZERO await
points inside, so on a single event loop each mutation runs to completion without
yielding. ``get()`` reserves the user's slot at pop time (not later), which is what makes
per-user caps exact even with many workers racing for the head of the queue.

Full-group behaviour (2026-10 requirement): a full group does NOT reject instantly.
``enqueue`` waits up to ``QUEUE_FULL_GRACE_SEC`` for space; if a slot frees it accepts,
otherwise it returns a RETRYABLE "server is full" result. No penalty, no ban - the
caller refunds the rate-limit token and the client simply resends.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple

from config import (
    QUEUE_MAX_PENDING,
    QUEUE_STATS_LOG_INTERVAL_SEC,
    QUEUE_FULL_GRACE_SEC,
    QUEUE_FULL_RETRY_AFTER_SEC,
)
from .request import ChatRequest

logger = logging.getLogger("COOLEMS.Provider.Queue")

# Exact wording the client shows when a model group's backlog is full.
QUEUE_FULL_MESSAGE = "Resend request in a min as server is full"


@dataclass
class EnqueueResult:
    """Outcome of RequestQueue.enqueue()."""

    accepted: bool
    reason: str = ""
    position: int = 0
    retryable: bool = False
    retry_after_sec: int = 0
    model_key: str = ""


class _LegacyQueueView:
    """Read-only stand-in for the old asyncio.Queue attribute (test/tooling compatibility)."""

    def __init__(self, queue: "RequestQueue"):
        self._q = queue

    def empty(self) -> bool:
        return self._q.pending_count() == 0


class RequestQueue:
    """Per-model bounded FIFOs with owner-scoped cancellation and per-user caps."""

    def __init__(self, max_pending: int = QUEUE_MAX_PENDING,
                 grace_sec: float = QUEUE_FULL_GRACE_SEC):
        self._max_pending = max(1, int(max_pending))
        self._grace_sec = max(0.0, float(grace_sec))
        self._by_model: dict[str, list[ChatRequest]] = {}
        self._by_owner: dict[str, deque] = {}
        # Running requests by req_id (NOT by owner): a buggy/duplicate client could have
        # two requests in flight for the same conversation, and cancellation must stop
        # every one of them.
        self._running: dict[str, ChatRequest] = {}
        self._running_by_user: dict[str, int] = {}
        self._running_by_model: dict[str, int] = {}
        self._cond = asyncio.Condition()
        self._stats = {"total_enqueued": 0, "total_rejected": 0, "total_served": 0}
        self._log_task: Optional[asyncio.Task] = None

    # --- compatibility view ------------------------------------------------------

    @property
    def _queue(self) -> _LegacyQueueView:
        return _LegacyQueueView(self)

    # --- helpers -----------------------------------------------------------------

    @property
    def max_pending(self) -> int:
        return self._max_pending

    @staticmethod
    def _user_of(req: ChatRequest) -> str:
        return req.user_key or req.owner_key

    def _eligible(self, req: ChatRequest) -> bool:
        """User cap check only (model is implied by the deque we scan)."""
        cap = int(req.max_parallel or 0)
        if cap <= 0:
            return True
        return self._running_by_user.get(self._user_of(req), 0) < cap

    def _remove_from_owner(self, req: ChatRequest) -> None:
        dq = self._by_owner.get(req.owner_key)
        if not dq:
            return
        try:
            dq.remove(req)
        except ValueError:
            pass
        if not dq:
            self._by_owner.pop(req.owner_key, None)

    def _unindex(self, req: ChatRequest) -> None:
        """Remove a queued request from both indexes (lock must be held)."""
        lst = self._by_model.get(req.model_key)
        if lst:
            try:
                lst.remove(req)
            except ValueError:
                pass
            if not lst:
                self._by_model.pop(req.model_key, None)
        self._remove_from_owner(req)

    def _dec(self, counts: dict, key: str) -> None:
        if not key:
            return
        left = counts.get(key, 0) - 1
        if left > 0:
            counts[key] = left
        else:
            counts.pop(key, None)

    # --- core ---------------------------------------------------------------------

    def pending_count(self, model_key: Optional[str] = None) -> int:
        """Requests waiting right now (optionally only for one model group)."""
        if model_key is None:
            return sum(len(v) for v in self._by_model.values())
        return len(self._by_model.get(model_key, ()))

    async def enqueue(self, req: ChatRequest) -> EnqueueResult:
        """Add *req* to its model group's backlog.

        A full group is given ``grace_sec`` to free a slot (a worker popping the next
        request opens one). If it does not, the result is RETRYABLE: the caller sends
        QUEUE_FULL_MESSAGE and refunds the rate-limit token. Nothing is counted against
        the user and no backend is blamed.
        """
        if req.is_cancelled():
            return EnqueueResult(False, "already cancelled", model_key=req.model_key)
        if not req.model_key:
            return EnqueueResult(False, "no model specified in the request", model_key="")

        key = req.model_key
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._grace_sec

        def _closed() -> bool:
            return bool(getattr(req.sink, "is_closed", False))

        async with self._cond:
            while len(self._by_model.get(key, ())) >= self._max_pending:
                if req.is_cancelled() or _closed():
                    return EnqueueResult(False, "cancelled while waiting for queue space",
                                         model_key=key)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self._stats["total_rejected"] += 1
                    logger.warning(
                        "[QUEUE] Group '%s' full (%d pending) - rejected request %s "
                        "(conv=%s, user=%s) with a retryable error",
                        key, self._max_pending, req.req_id, req.conv_id or "?",
                        self._user_of(req),
                    )
                    return EnqueueResult(
                        False, QUEUE_FULL_MESSAGE, retryable=True,
                        retry_after_sec=QUEUE_FULL_RETRY_AFTER_SEC, model_key=key,
                    )
                try:
                    # Short slices keep cancellation / closed-socket checks responsive.
                    await asyncio.wait_for(self._cond.wait(), timeout=min(0.5, remaining))
                except asyncio.TimeoutError:
                    pass

            lst = self._by_model.setdefault(key, [])
            lst.append(req)
            self._by_owner.setdefault(req.owner_key, deque()).append(req)
            self._stats["total_enqueued"] += 1
            position = len(lst)
            if position > 1:
                logger.info("[QUEUE] Request %s (model=%s conv=%s user=%s) queued at %d",
                            req.req_id, key, req.conv_id or "?", self._user_of(req), position)
            self._cond.notify_all()

        return EnqueueResult(True, f"position {position}", position=position, model_key=key)

    async def get(self, model_key: str) -> ChatRequest:
        """Wait for the next request of *model_key* whose user may take a slot.

        The user's slot is RESERVED here (running counters incremented) so two workers
        can never admit two requests for a user whose cap is 1. Callers MUST always call
        :meth:`mark_done` for a returned request (the worker does it in a finally block).
        """
        async with self._cond:
            while True:
                lst = self._by_model.get(model_key)
                if lst:
                    for idx, req in enumerate(lst):
                        if self._eligible(req):
                            lst.pop(idx)
                            if not lst:
                                self._by_model.pop(model_key, None)
                            self._remove_from_owner(req)

                            # Reserve the slot atomically with the pop.
                            self._running[req.req_id] = req
                            ukey = self._user_of(req)
                            self._running_by_user[ukey] = self._running_by_user.get(ukey, 0) + 1
                            self._running_by_model[model_key] = \
                                self._running_by_model.get(model_key, 0) + 1
                            self._cond.notify_all()  # queue space freed / caps changed
                            return req
                await self._cond.wait()

    # --- lifecycle bookkeeping (called by the worker pool) -------------------------

    async def mark_done(self, req: ChatRequest) -> None:
        """Release the slot reserved by get(); wakes users waiting on their cap."""
        async with self._cond:
            self._running.pop(req.req_id, None)
            self._dec(self._running_by_user, self._user_of(req))
            self._dec(self._running_by_model, req.model_key)
            if not req.is_cancelled():
                self._stats["total_served"] += 1
            self._cond.notify_all()

    # --- cancellation ---------------------------------------------------------------

    async def cancel_owner(self, owner_key: str) -> int:
        """Abort everything owned by *owner_key*: queued entries are removed in place
        (both indexes) and the running one (if any) gets its stop event set."""
        removed = 0
        async with self._cond:
            dq = self._by_owner.pop(owner_key, None)
            if dq is not None:
                for item in list(dq):
                    if not item.is_cancelled():
                        if item.stop_event is not None:
                            item.stop_event.set()
                        removed += 1
                    self._unindex(item)
            running = [r for r in self._running.values() if r.owner_key == owner_key]
            for req in running:
                if req.is_cancelled():
                    continue
                if req.stop_event is not None:
                    req.stop_event.set()
                removed += 1
            self._cond.notify_all()
        if removed:
            logger.info("[QUEUE] Cancelled %d request(s) for owner=%s", removed, owner_key)
        return removed

    async def expire(self, predicate) -> list[ChatRequest]:
        """Remove queued requests matching *predicate* (sync callable, lock held).

        Used by the worker-pool watchdog for "model configured but all its nodes are
        down" timeouts. The caller sends the error frames (we never await inside the
        lock). Running requests are untouched.
        """
        expired: list[ChatRequest] = []
        async with self._cond:
            for key in list(self._by_model.keys()):
                keep = []
                for req in self._by_model[key]:
                    if predicate(req):
                        expired.append(req)
                    else:
                        keep.append(req)
                if keep:
                    self._by_model[key] = keep
                else:
                    self._by_model.pop(key, None)
            for req in expired:
                self._remove_from_owner(req)
            if expired:
                self._cond.notify_all()
        return expired

    # --- observability -----------------------------------------------------------------

    async def drain_and_cancel_all(self) -> int:
        """Remove every queued request and set their stop events (shutdown path)."""
        async with self._cond:
            aborted = sum(len(v) for v in self._by_model.values())
            for lst in self._by_model.values():
                for item in lst:
                    if item.stop_event is not None and not item.stop_event.is_set():
                        item.stop_event.set()
            self._by_model.clear()
            self._by_owner.clear()
            self._cond.notify_all()
        return aborted

    def stats(self) -> dict:
        by_model = {}
        for key in set(self._by_model) | set(self._running_by_model):
            by_model[key] = {
                "pending": len(self._by_model.get(key, ())),
                "running": self._running_by_model.get(key, 0),
            }
        return {
            "pending": self.pending_count(),
            "running": len(self._running),
            "max_pending_per_model": self._max_pending,
            "by_model": by_model,
            **self._stats,
        }

    def start_stats_logger(self, interval_sec: int = QUEUE_STATS_LOG_INTERVAL_SEC) -> None:
        """Periodic queue-depth heartbeat - INFO only when state CHANGES.

        interval <= 0 disables it; start/stop stay idempotent.
        """
        if interval_sec and interval_sec > 0 and self._log_task is None:
            last_logged = [None]

            def _snapshot() -> tuple:
                s = self.stats()
                models = tuple(sorted(
                    (k, v["pending"], v["running"]) for k, v in s["by_model"].items()
                ))
                return (s["pending"], s["running"], s["total_served"],
                        s["total_rejected"], models)

            async def _loop():
                while True:
                    await asyncio.sleep(interval_sec)
                    snap = _snapshot()
                    if last_logged[0] is None or snap != last_logged[0]:
                        level, last_logged[0] = logging.INFO, snap
                    else:
                        level = logging.DEBUG
                    logger.log(
                        level,
                        "[QUEUE] pending=%d running=%d served=%d rejected=%d per-model=%s",
                        snap[0], snap[1], snap[2], snap[3], snap[4],
                    )

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


def set_queue(queue: Optional[RequestQueue]) -> None:
    global _queue
    _queue = queue


def get_queue() -> Optional[RequestQueue]:
    return _queue
