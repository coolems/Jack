"""WorkerPool — one worker PER INFERENCE SLOT, pulling from that model group's queue.

2026-09-08 multi-chat (Phase 2); 2026-10 fleet: a backend is no longer one worker, and
the queue is no longer global. Each llama-server exposes N slots (--parallel N,
discovered via /props.total_slots), so the pool runs N workers bound to that backend.
Each worker only takes requests from ITS MODEL GROUP, so a Qwen slot never runs a Gemma
request and vice versa.

This is "send it to the first available llama of that model" by construction: every free
slot has a worker waiting on its group's FIFO. With one configured llama (concurrency 1)
this degrades exactly to the old single-server behaviour.

Live reconciliation (called from the watchdog every 5 s):
  * a node that reports MORE slots gets additional workers;
  * a node that reports FEWER slots has its extra workers retired (a busy one finishes
    its current request first - we never abort running work);
  * a node whose model changed is fully respawned on the new model group (workers waiting
    on the old queue key would otherwise never see the new model's requests).

Health gating: a worker waits until ITS backend is healthy BEFORE taking work, so an
unreachable node never consumes a queued request just to answer "backend unavailable".

Watchdog: also expires queued requests whose model has NO healthy node for longer than
QUEUE_MODEL_WAIT_SEC and sends a clear, retryable error (the "bad luck" path). Requests
for a model that is merely busy are never expired - they wait FIFO.

Cancellation model: a ChatRequest carries its owner-scoped stop event. A request
cancelled while QUEUED is skipped by the worker that pops it; one cancelled while
RUNNING aborts llama generation via the same event passed to chat_stream.
"""

import asyncio
import logging
from typing import Optional

from config import CONTEXT_WINDOW_TOKENS, QUEUE_MODEL_WAIT_SEC
from .queue_manager import RequestQueue
from .request import ChatRequest

# Bound at IMPORT time on purpose: the chat core is stable once this module loads, and
# tests patch worker_pool.run_chat_core (module attribute) instead of re-importing.
from app.providers.coolems.chat_relay import run_chat_core

logger = logging.getLogger("COOLEMS.Provider.Queue.Workers")


class WorkerPool:
    """N workers per backend (N = that backend's discovered concurrency)."""

    def __init__(self, registry, queue: RequestQueue):
        self._registry = registry
        self._queue = queue
        self._tasks: list[asyncio.Task] = []
        self._workers: dict[tuple, list[dict]] = {}   # backend.key -> worker states
        self._next_slot: dict[tuple, int] = {}
        self._watchdog: Optional[asyncio.Task] = None
        self._started = False

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Spawn one worker per inference slot of every backend (idempotent)."""
        if self._started:
            return
        for b in self._registry.backends.values():
            for _ in range(max(1, int(b.capacity))):
                self._spawn_worker(b)
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="queue-watchdog")
        self._started = True
        logger.info(
            "[WORKERS] Started %d worker(s) across %d backend(s): %s",
            len(self._tasks),
            len(self._registry.backends),
            ", ".join(f"{b.label} x{b.capacity}" for b in self._registry.backends.values()),
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Cancel all workers + watchdog and wait briefly for them to wind down."""
        if not self._started:
            return
        if self._watchdog is not None and not self._watchdog.done():
            self._watchdog.cancel()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        all_tasks = self._tasks + ([self._watchdog] if self._watchdog else [])
        if all_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*all_tasks, return_exceptions=True), timeout=timeout
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning("[WORKERS] Worker shutdown issue: %s", e)
        self._tasks = []
        self._workers = {}
        self._next_slot = {}
        self._watchdog = None
        self._started = False
        logger.info("[WORKERS] All workers stopped")

    # --- spawning / reconciliation ------------------------------------------------

    def _spawn_worker(self, backend) -> None:
        key = backend.key
        slot = self._next_slot.get(key, 0) + 1
        self._next_slot[key] = slot
        state = {
            "slot": slot,
            "busy": False,
            "retire": False,
            "model_key": backend.model_key,
            "task": None,
        }
        task = asyncio.create_task(
            self._worker_loop(backend, state),
            name=f"llm-worker-{backend.instance.name}-{slot}",
        )
        state["task"] = task
        self._workers.setdefault(key, []).append(state)
        self._tasks.append(task)

    @staticmethod
    def _retire(state: dict) -> None:
        """Ask a worker to stop: idle ones are cancelled, busy ones finish their request."""
        state["retire"] = True
        task = state.get("task")
        if task is not None and not task.done() and not state.get("busy"):
            task.cancel()

    def reconcile(self) -> None:
        """Match the worker count/model of every backend to its CURRENT /props values."""
        # Drop finished tasks from the bookkeeping list (retired workers accumulate).
        self._tasks = [t for t in self._tasks if not t.done()]
        for key, backend in self._registry.backends.items():
            states = [s for s in self._workers.get(key, []) if not s["task"].done()]
            self._workers[key] = states

            # Model changed under us (node restarted with another model): respawn all.
            if states and any(s["model_key"] != backend.model_key for s in states):
                logger.info("[WORKERS] %s model changed to '%s' - respawning its workers",
                            backend.label, backend.model_key or "?")
                for s in states:
                    self._retire(s)
                states = []
                self._workers[key] = []

            want = max(1, int(backend.capacity))
            if len(states) < want:
                for _ in range(want - len(states)):
                    self._spawn_worker(backend)
                logger.info("[WORKERS] %s: %d -> %d worker(s) (slots)",
                            backend.label, len(states), want)
            elif len(states) > want:
                for s in states[want:]:
                    self._retire(s)
                logger.info("[WORKERS] %s: %d -> %d worker(s) (slots); extras finish current work",
                            backend.label, len(states), want)

    # --- worker loop -----------------------------------------------------------

    async def _worker_loop(self, backend, state: dict) -> None:
        """Take requests of THIS backend's model group forever (until retired)."""
        while not state.get("retire"):
            # Do not consume work for a node that cannot serve it. The event wakes us
            # when the health poller marks the backend healthy again.
            if not backend.healthy:
                if not await backend.wait_healthy():
                    continue
            if state.get("retire"):
                break

            req: ChatRequest = await self._queue.get(backend.model_key)
            req.backend = backend.label
            req.context_window = int(backend.ctx_per_slot or CONTEXT_WINDOW_TOKENS)
            state["busy"] = True
            try:
                await self._execute(backend, req)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - defensive: worker must never die
                backend.bump_error(f"{type(e).__name__}: {e}")
                logger.error("[WORKERS] Worker %s slot %s crashed on request %s: %s",
                             backend.label, state.get("slot"), req.req_id, e, exc_info=True)
            finally:
                # get() reserved this user's slot; always release it (also on early returns).
                await self._queue.mark_done(req)
                state["busy"] = False

    async def _execute(self, backend, req: ChatRequest) -> None:
        """Run one queued request end-to-end (frames flow through req.sink)."""
        # Cancelled while still in the queue — skip without touching the backend.
        if req.is_cancelled():
            logger.info("[WORKERS] Skipping cancelled request %s (conv=%s)", req.req_id, req.conv_id or "?")
            await self._send(req.sink, {"type": "cancelled", "req_id": req.req_id})
            return

        # Backend became unhealthy between queueing and pickup: fail fast with a clear
        # frame. (Health-gated workers make this a narrow race, not the normal path.)
        if not backend.healthy:
            logger.warning("[WORKERS] Request %s (conv=%s) dropped — backend %s is unhealthy",
                           req.req_id, req.conv_id or "?", backend.label)
            backend.bump_error("request dropped: backend unhealthy at pickup")
            await self._send(req.sink, {
                "type": "error",
                "message": "LLM backend is unavailable - please retry.",
                "retryable": True,
                "backend": backend.info(),
            })
            return

        # Routing safety (2026-10 fleet): never run a request on a node whose discovered
        # model group differs. The queue already filters by model, so this only fires if
        # a node was restarted with another model between two /props polls.
        if req.model_key and backend.model_key and req.model_key != backend.model_key:
            msg = (f"Backend {backend.label} serves '{backend.model_label}', not "
                   f"'{req.model}'. Routing mismatch - restart that node with the model.")
            backend.bump_error(msg)
            logger.error("[WORKERS] %s", msg)
            await self._send(req.sink, {
                "type": "error", "message": msg, "retryable": True,
                "backend": backend.info(),
            })
            return

        # Tell the chat its turn started (UI can drop the "queued" badge).
        if req.conv_id:
            await self._send(req.sink, {"type": "queue_start", "conv_id": req.conv_id})

        backend.acquire()
        try:
            delivered = await run_chat_core(
                None,  # server_provider unused by the core except for logging context
                backend.provider(), req.sink,
                model=req.model, messages=req.messages, temperature=req.temperature,
                enable_thinking=req.enable_thinking, conv_id=req.conv_id, tools=req.tools,
                stop_event=req.stop_event, context_window=req.context_window,
            )
            if not delivered and req.is_cancelled():
                await self._send(req.sink, {"type": "cancelled", "req_id": req.req_id})
        except Exception as e:
            backend.bump_error(f"{type(e).__name__}: {e}")
            logger.error("[WORKERS] Request %s failed on %s: %s",
                         req.req_id, backend.label, e, exc_info=True)
            await self._send(req.sink, {
                "type": "error",
                "message": f"Request failed on {backend.label}: {e}",
                "retryable": True,
                "backend": backend.info(),
            })
        finally:
            backend.release()

    # --- watchdog: capacity reconcile + "model configured but all nodes down" ------

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            try:
                # Keep worker counts/models in sync with what each node reports.
                self.reconcile()

                def _expired(req: ChatRequest) -> bool:
                    return (req.age_sec > QUEUE_MODEL_WAIT_SEC
                            and not self._registry.model_healthy(req.model_key))

                expired = await self._queue.expire(_expired)
                for req in expired:
                    logger.warning(
                        "[WATCHDOG] Expiring request %s (model=%s conv=%s): no healthy node "
                        "for %ds. Fleet: %s",
                        req.req_id, req.model_key, req.conv_id or "?",
                        QUEUE_MODEL_WAIT_SEC, self._registry.describe_models(),
                    )
                    await self._send(req.sink, {
                        "type": "error",
                        "message": (f"No llama node is serving '{req.model}' right now - "
                                    f"please resend in a minute."),
                        "retryable": True,
                        "retry_after_sec": 60,
                        "model": req.model_key,
                    })
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover - watchdog must never die
                logger.error("[WATCHDOG] error: %s", e, exc_info=True)

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


def set_worker_pool(pool: Optional[WorkerPool]) -> None:
    global _pool
    _pool = pool


def get_worker_pool() -> Optional[WorkerPool]:
    return _pool
