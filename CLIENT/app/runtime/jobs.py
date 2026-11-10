"""JobRegistry — one kind-independent background-job runtime (client side, 2026-12).

The runtime owns identity, lifecycle state and the output ring; the PRODUCER owns the
execution resources. A producer declares a JobSpec and returns hooks:

    spec = JobSpec(kind="bash", label="pytest tests", owner=conv_id, run=starter)
    job_id = registry.start(spec)          # returns immediately, never awaits
    hooks  = JobHooks(cancel=..., done=asyncio.Future/Task)

Contract (deliberately mirrors the harness design we studied):
  * id = "<kind>-<short-uuid>" — predictable on purpose; authorization, not secrecy,
    is the boundary. Access is fenced by the OWNER conversation id.
  * the model consumes the ring through a registry-kept cursor; UI observers read at
    absolute offsets. Neither disturbs the other.
  * settlement is FIRST-WINS: one terminal record, released waiters, one notice.
  * a settled record stays listed until its owner is disposed or `remove()` is called by
    a caller that collected the terminal state itself and never handed the id out.
  * pull sources are pumped by the registry at a fixed cadence and drained ONE last time
    before settlement closes the ring, so a producer never has to fold output into done.
  * turn completion does NOT imply jobs stopped: a job outlives the turn that started it,
    which is exactly what makes "start 4 builds, keep working" possible.

This module is process-local. Durability (job rows in SQLite) lives in runtime_store.py;
a job whose settlement never arrived stays "incomplete" — we never invent a finish time.
"""

import asyncio
import inspect
import logging
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from config import (
    JOB_LIVE_RETAIN_BYTES,
    JOB_SETTLED_RETAIN_BYTES,
    JOB_MAX_CONCURRENT_PER_CONV,
    JOB_PUMP_POLL_MS,
)

from .output_ring import OutputRing

logger = logging.getLogger("COOLEMS.Runtime.Jobs")

#: Terminal outcomes. 'killed' = someone asked; 'failed' = it broke.
STATUSES = ("running", "stopping", "completed", "killed", "failed")


class JobOutcome:
    """Terminal result a producer reports through its done awaitable.

    `cause` is the registry's own reason label ('producer' | 'kill' | 'teardown'), so a
    job that ended because the runtime shut down is never reported as a producer failure.
    """

    __slots__ = ("status", "detail", "result", "cause")

    def __init__(self, status: str = "completed", detail: str = "", result: str = "",
                 cause: str = ""):
        self.status = status if status in ("completed", "killed", "failed") else "failed"
        self.detail = str(detail or "")
        self.result = str(result or "")
        self.cause = str(cause or "")


class JobHandle:
    """Producer face: write output + progress. All synchronous, valid for the job's life."""

    def __init__(self, registry: "JobRegistry", job_id: str):
        self._registry = registry
        self.id = job_id

    def append(self, text: str, channel: str = "stdout", gap_before: bool = False) -> None:
        self._registry._append(self.id, text, channel, gap_before)

    def update_progress(self, line: str) -> None:
        self._registry._set_progress(self.id, str(line or ""))


class JobHooks:
    """Runtime's control face over producer work."""

    def __init__(self, cancel: Callable[[Optional[str]], None],
                 done: Awaitable[JobOutcome]):
        self.cancel = cancel
        self.done = done


class JobSpec:
    """Producer declaration for registry.start()."""

    def __init__(self, kind: str, label: str, owner: Optional[str] = None,
                 run: Callable[[JobHandle], JobHooks] = None,
                 output_sources: Optional[List[Any]] = None,
                 tool_call_id: str = "", output_limit_bytes: int = 0):
        self.kind = str(kind or "job")
        self.label = str(label or "")
        self.owner = owner or None
        self.run = run
        self.output_sources = list(output_sources or [])
        self.tool_call_id = tool_call_id or ""
        self.output_limit_bytes = int(output_limit_bytes or 0)


class JobRecord:
    """One registered job: identity, lifecycle, ring, cursor, hooks."""

    def __init__(self, spec: JobSpec, job_id: str, ring: OutputRing):
        self.id = job_id
        self.kind = spec.kind
        self.label = spec.label
        self.owner = spec.owner
        self.tool_call_id = spec.tool_call_id
        self.output_limit_bytes = spec.output_limit_bytes
        self.ring = ring
        self.cursor = 0                 # the MODEL's consuming cursor
        self.status = "running"
        self.progress = ""
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.detail = ""
        self.result = ""
        self.producer_reported = False
        self.cause = ""                 # 'producer' | 'kill' | 'teardown'
        self.hooks: Optional[JobHooks] = None
        self.sources: List[Any] = []
        self.pump_task: Optional[asyncio.Task] = None
        self.watcher: Optional[asyncio.Task] = None
        self.waiters: List[asyncio.Future] = []
        self.notified = False           # one settlement notice per job, deduped
        self.result_handed_out = False  # JobOutcome.result is handed out exactly once
        # (2026-12) Closes the spill file handle at settlement. Without it one open file
        # handle leaked per job (and Windows keeps the file locked until close).
        self.spill_close: Optional[Callable[[], None]] = None
        # (2026-12) True once the ledger row/UI row for this settlement was recorded, so
        # a late kill or a teardown never writes a second row for the same job id.
        self.row_recorded = False
        # (2026-11-30) CLAIM: the model already received this job's TERMINAL STATE through
        # a tool result - job_output(wait=true) released on settlement, a read that handed
        # out the result, job_kill's own 'is stopping' answer, or remove() collecting it.
        # A settlement whose state was already delivered announces nothing (the harness
        # delivery ledger). A job that settles while nobody is reading it is NOT claimed:
        # the notice is the only delivery, so it must fire - and it never carries the
        # result payload, which job_output owns. Teardown never claims either: the model
        # must learn its background work was cut short.
        self.claimed = False

    def view(self) -> Dict:
        """Observer-safe projection (no output payloads)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "owner": self.owner or "",
            "status": self.status,
            "progress": self.progress,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "detail": self.detail,
            "cause": self.cause,
            "unread_bytes": max(0, self.ring.total - self.cursor),
            "has_result": bool(self.result) and not self.result_handed_out,
            **self.ring.view(),
        }


class JobRegistry:
    """conv_id -> JobRecord registry with pump + settlement semantics."""

    def __init__(self, *, spill_dir: Optional[str] = None,
                 live_retain: int = JOB_LIVE_RETAIN_BYTES,
                 settled_retain: int = JOB_SETTLED_RETAIN_BYTES,
                 max_per_owner: int = JOB_MAX_CONCURRENT_PER_CONV,
                 pump_poll_ms: int = JOB_PUMP_POLL_MS):
        self._jobs: Dict[str, JobRecord] = {}
        # Per-job consuming locks (2026-11-30): two job_output calls in one batch must not
        # consume one another's bytes. Built lazily on the running loop.
        self._read_locks: Dict[str, asyncio.Lock] = {}
        # Pending settlement announcements (deferred one loop step so a released waiter can
        # claim the job before the notice decides). Kept referenced so they never vanish.
        self._announce_tasks: set = set()
        # (2026-12) Pending ledger/UI rows (same deferred shape as the announcements, kept
        # in their own bucket so a teardown can await rows and notices separately).
        self._record_tasks: set = set()
        # (2026-12) Fire-and-forget tasks (UI output frames, the start observer). asyncio
        # keeps NO strong reference to a task, so an unscheduled-suspended one can be
        # garbage-collected mid-flight; they are referenced here and dropped when done.
        self._transient_tasks: set = set()
        self._spill_dir = spill_dir
        self._live_retain = max(1, int(live_retain))
        self._settled_retain = max(1, int(settled_retain))
        self._max_per_owner = max(1, int(max_per_owner))
        self._pump_poll = max(0.02, pump_poll_ms / 1000.0)
        self._on_settle: Optional[Callable[[JobRecord], Awaitable[None]]] = None
        # (2026-12) Ledger/UI recorder that runs on EVERY settlement, claimed or not. The
        # notice is about what the MODEL already received; the row is about what the USER
        # and a reload must see. Gating the row on `claimed` (the old shape) meant a
        # job_kill left NO ledger row and left the UI's job row stuck at 'running'.
        self._on_record: Optional[Callable[[JobRecord], Awaitable[None]]] = None
        self._on_output: Optional[Callable[[JobRecord, str, str], Awaitable[None]]] = None
        self._on_start: Optional[Callable[[JobRecord], Awaitable[None]]] = None
        self._closed = False
        # (2026-11-30) The loop this registry LIVES on, captured at construction (startup
        # runs on the uvicorn loop). Producers append output from their OWN worker thread,
        # so an observer callback cannot use create_task() there - it must be marshalled
        # back onto the home loop. None = built off-loop (tests / direct use), where the
        # current-thread behaviour stays as before.
        try:
            self._home_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._home_loop = None

    # --- wiring ----------------------------------------------------------

    def set_settlement_handler(self, handler: Callable[[JobRecord], Awaitable[None]]) -> None:
        """Async callback fired once per settlement — the notice lane plugs in here."""
        self._on_settle = handler

    def set_record_handler(self, handler: Callable[[JobRecord], Awaitable[None]]) -> None:
        """Async callback for the durable ledger row + UI row of EVERY settlement.

        Separate from set_settlement_handler on purpose: the notice is a delivery to the
        MODEL and must stay silent when a tool result already delivered the outcome
        (job_output / job_kill), but the ledger row and the UI row are not a delivery -
        they are the record the user sees on reload. Firing the row only from the notice
        handler meant a killed job had no row at all and its UI row stayed 'running'.
        """
        self._on_record = handler

    def set_output_observer(self, observer: Callable[[JobRecord, str, str], Awaitable[None]]) -> None:
        """Async callback for live output chunks (UI frames). Never moves the model cursor."""
        self._on_output = observer

    def set_start_observer(self, observer: Callable[[JobRecord], Awaitable[None]]) -> None:
        """Async callback fired once when a job STARTS (2026-12 UI row).

        Best-effort: a failing observer must not undo a started job, so it is scheduled as a
        task and never awaited inside start() — start() must return the id synchronously.
        """
        self._on_start = observer

    # --- registration ----------------------------------------------------

    def _owner_count(self, owner: Optional[str]) -> int:
        if not owner:
            return 0
        return sum(1 for j in self._jobs.values()
                   if j.owner == owner and j.status in ("running", "stopping"))

    def start(self, spec: JobSpec) -> str:
        """Preflight, start work, register atomically. Returns the job id immediately.

        A throwing starter leaves NOTHING registered (no id, no half-started record); the
        producer must clean up anything it partially started.
        """
        if self._closed:
            raise RuntimeError("job registry is closed")
        if spec.run is None:
            raise ValueError("JobSpec.run is required")
        if spec.owner and self._owner_count(spec.owner) >= self._max_per_owner:
            raise RuntimeError(
                f"owner {spec.owner} already has {self._max_per_owner} live jobs - "
                "stop one with job_kill before starting another")

        job_id = f"{spec.kind}-{uuid.uuid4().hex[:8]}"
        spill_path, spill_writer, spill_close = self._spill_path_for(job_id)
        ring = OutputRing(self._live_retain, spill_path=spill_path,
                          spill_writer=spill_writer)
        rec = JobRecord(spec, job_id, ring)
        rec.spill_close = spill_close
        # Registration is visible BEFORE run() returns, so a producer that appends inside
        # its starter is not writing into a job nobody can find yet.
        self._jobs[job_id] = rec
        try:
            hooks = spec.run(JobHandle(self, job_id))
        except Exception:
            self._jobs.pop(job_id, None)
            raise
        if hooks is None or getattr(hooks, "done", None) is None:
            self._jobs.pop(job_id, None)
            raise ValueError("JobHooks.done is required")
        rec.hooks = hooks
        rec.sources = list(spec.output_sources)
        if rec.sources:
            rec.pump_task = asyncio.create_task(self._pump(rec), name=f"job-pump-{job_id}")
        watcher = asyncio.create_task(self._watch(rec), name=f"job-watch-{job_id}")
        rec.watcher = watcher  # type: ignore[attr-defined]
        # (2026-12 UI) fire the start observer on the home loop: start() must return the
        # id synchronously, and a failing UI frame must never undo a started job.
        if self._on_start is not None:
            self._schedule(lambda: self._fire_start(rec))
        logger.info(f"[JOBS] started {job_id} owner={spec.owner or '-'} label={spec.label[:80]}")
        return job_id

    async def _fire_start(self, rec: JobRecord) -> None:
        try:
            await self._on_start(rec)
        except Exception as exc:  # pragma: no cover - UI gone; the job keeps running
            logger.debug(f"[JOBS] start observer failed for {rec.id}: {exc!r}")

    def _spill_path_for(self, job_id: str) -> Tuple[Optional[str], Optional[Callable[[str], None]],
                                                  Optional[Callable[[], None]]]:
        """(path, writer, closer) for the COMPLETE stream of one job.

        (2026-12 fix) The writer existed but was never handed to the ring, so every spill
        file stayed 0 bytes while job_output and the settlement notice both told the model
        "[complete stream file: <path>]" - a locator to an empty file. That is a lie about
        data the model cannot recover: once the ring evicts the head, the spill file is the
        ONLY complete copy. Now the ring writes every chunk through it, and the handle is
        closed at settlement (never leaked per job).
        """
        if not self._spill_dir:
            return None, None, None
        import os
        try:
            os.makedirs(self._spill_dir, exist_ok=True)
            path = os.path.join(self._spill_dir, f"{job_id}.log")
            # newline="" - the ring hands us text that ALREADY carries its own line
            # endings (a sandboxed child's print() emits "\n", and its stdout in text mode
            # may already be "\r\n"). Default text mode would translate "\n" -> "\r\n"
            # again, so every line landed as "\r\r\n": the complete-stream file was
            # byte-corrupted even though the ring and the notice were correct.
            handle = open(path, "a", encoding="utf-8", errors="replace", newline="")

            def _write(text: str) -> None:
                try:
                    handle.write(text)
                    handle.flush()
                except Exception:
                    pass  # a broken spill must never break the producer

            def _close() -> None:
                try:
                    handle.close()
                except Exception:
                    pass

            return path, _write, _close
        except Exception as exc:  # pragma: no cover - a spill must never break a job
            logger.debug(f"[JOBS] spill unavailable for {job_id}: {exc}")
            return None, None, None

    # --- reads -----------------------------------------------------------

    def _owned(self, job_id: str, caller: Optional[str]) -> JobRecord:
        rec = self._jobs.get(job_id)
        if rec is None:
            raise KeyError(f"unknown job id: {job_id}")
        if rec.owner and caller and rec.owner != caller:
            raise PermissionError(f"job {job_id} belongs to another conversation")
        return rec

    def get(self, job_id: str, caller: Optional[str] = None) -> Dict:
        return self._owned(job_id, caller).view()

    def list(self, caller: Optional[str] = None) -> List[Dict]:
        """Caller-owned + unowned jobs, registration order."""
        out = []
        for rec in self._jobs.values():
            if rec.owner and caller and rec.owner != caller:
                continue
            if caller is None and rec.owner:
                continue
            out.append(rec.view())
        return out

    async def consume(self, job_id: str, caller: Optional[str] = None,
                      wait_ms: float = 0.0,
                      steer_check: Optional[Callable[[], bool]] = None) -> Tuple[Dict, bool, Dict]:
        """One consuming read, serialized per job (2026-11-30).

        job_output's shape is 'wait (maybe), then read'. Two such calls dispatched in the
        SAME batch would share one cursor and consume one another's output: reader A gets
        bytes 0..N, reader B then gets N..M and neither sees the whole stream. A per-job
        lock makes them take turns, so each reader receives a distinct complete chunk and
        no bytes are lost or duplicated.

        Returns (view, steered, read). A steer during the wait is reported, never hidden.
        """
        lock = self._read_lock(job_id)
        async with lock:
            view, steered = await self.wait(job_id, wait_ms, caller, steer_check=steer_check)
            if steered:
                # The job is still live and the user redirected the task - do not consume
                # bytes here; the next read should see everything up to that point.
                return view, True, {"text": "", "lossy": False, "result": "", "job": view}
            return view, False, self.read(job_id, caller)

    def _read_lock(self, job_id: str) -> asyncio.Lock:
        """Per-job consuming lock, built on the running loop (never off-loop)."""
        lock = self._read_locks.get(job_id)
        if lock is None:
            lock = asyncio.Lock()
            self._read_locks[job_id] = lock
        return lock

    def read(self, job_id: str, caller: Optional[str] = None) -> Dict:
        """Consume the ring from the MODEL cursor and advance it.

        After settlement the first read also carries the producer's result (once), and that
        hand-out CLAIMS the job (2026-11-30): the model already received the terminal state
        through this tool result, so the settlement notice must not deliver it a second time.
        """
        rec = self._owned(job_id, caller)
        text, lossy, next_cursor = rec.ring.read_text(rec.cursor)
        rec.cursor = next_cursor
        result = ""
        if rec.status in ("completed", "killed", "failed") and rec.result and not rec.result_handed_out:
            result = rec.result
            rec.result_handed_out = True
            rec.claimed = True
        return {
            "text": text,
            "lossy": lossy,
            "result": result,
            "job": rec.view(),
        }

    def read_at(self, job_id: str, from_offset: int = 0,
                caller: Optional[str] = None) -> Dict:
        """Non-consuming read at an absolute offset (UI observers). Cursor untouched."""
        rec = self._owned(job_id, caller)
        text, lossy, next_offset = rec.ring.read_text(from_offset, include_log=True)
        return {"text": text, "lossy": lossy, "next": next_offset,
                "job": rec.view()}

    def unread(self, job_id: str, caller: Optional[str] = None,
               limit_bytes: int = 0) -> Dict:
        """Peek at unread bytes WITHOUT moving the cursor (settlement notices use this)."""
        rec = self._owned(job_id, caller)
        text, lossy, _ = rec.ring.read_text(rec.cursor)
        if limit_bytes and len(text.encode("utf-8", "replace")) > limit_bytes:
            text = text[-limit_bytes:]
            lossy = True
        return {"text": text, "lossy": lossy, "job": rec.view()}

    async def wait(self, job_id: str, timeout_ms: float,
                   caller: Optional[str] = None,
                   steer_check: Optional[Callable[[], bool]] = None) -> Dict:
        """Wait for settlement without cancelling. Yields early to a user steer.

        Returns (projection, steered) — a steer during a wait is reported so the tool
        result can say '[status: running, user steered]' instead of pretending the job
        finished.

        A FRESH waiter future per poll is required: asyncio.wait_for() cancels the future
        it is awaiting when its timeout expires, so re-using one future would raise
        CancelledError on the next poll — a bounded wait would read as a crashed tool call
        instead of an honest 'still running'.
        """
        rec = self._owned(job_id, caller)
        timeout = max(0.0, float(timeout_ms) / 1000.0)
        if rec.status in ("completed", "killed", "failed"):
            # A wait that found the job settled is a delivery too - claim it, so the
            # settlement notice does not repeat what the tool result already said.
            rec.claimed = True
            return rec.view(), False
        if timeout == 0.0:
            # 'no wait' must not become 'wait forever': a zero budget returns the current
            # state immediately. (The job_output tool applies its own default timeout
            # before calling, so 0 here always means 'do not wait'.)
            return rec.view(), False

        poll = min(0.5, self._pump_poll)
        deadline = (time.monotonic() + timeout) if timeout > 0 else None
        steered = False
        fut: Optional[asyncio.Future] = None
        try:
            while True:
                if deadline is not None and deadline - time.monotonic() <= 0:
                    break
                fut = asyncio.get_running_loop().create_future()
                rec.waiters.append(fut)
                try:
                    remaining = (deadline - time.monotonic()) if deadline is not None else poll
                    await asyncio.wait_for(fut, timeout=max(poll, min(remaining, poll)))
                    break
                except asyncio.TimeoutError:
                    pass
                finally:
                    if fut in rec.waiters:
                        rec.waiters.remove(fut)
                    fut = None
                if steer_check is not None and steer_check():
                    steered = True
                    break
        finally:
            if fut is not None and fut in rec.waiters:
                rec.waiters.remove(fut)
        if rec.status in ("completed", "killed", "failed"):
            # The wait RELEASED on settlement, so this tool result is the delivery of the
            # terminal state - claim it, so the notice lane does not announce it again.
            # A wait that timed out (still running) claims nothing: nobody has the outcome.
            rec.claimed = True
        return rec.view(), steered

    # --- control ---------------------------------------------------------

    def kill(self, job_id: str, caller: Optional[str] = None,
             reason: str = "", claim: bool = True) -> str:
        """Request cancellation. Returns 'requested' or 'already-finished'.

        `claim` says whether this call's own answer IS the delivery to the model. The
        job_kill TOOL claims (it already told the model 'is stopping'), so the later
        settlement must not announce the kill again. Runtime teardown (drain_owner /
        close) does NOT claim: nobody received anything, and the ledger row plus the
        notice are the only record that the job was ended by the runtime, not by the
        producer. A teardown notice is also what tells the model the work was cut short.
        """
        rec = self._owned(job_id, caller)
        if rec.status in ("completed", "killed", "failed"):
            return "already-finished"
        rec.status = "stopping"
        rec.cause = "kill"
        if claim:
            rec.claimed = True
        try:
            if rec.hooks is not None:
                rec.hooks.cancel(reason or "cancelled by the agent")
        except Exception as exc:  # a throwing cancel propagates as a failed job
            logger.warning(f"[JOBS] cancel({job_id}) raised {exc!r}")
            # Settlement work, so it goes in the awaited bucket: a teardown must not
            # return with this job's terminal state still unwritten.
            self._schedule(lambda: self._settle(
                rec, JobOutcome("failed", detail=f"cancel raised: {exc}")),
                bucket=self._announce_tasks)
        return "requested"

    def remove(self, job_id: str, caller: Optional[str] = None) -> None:
        """Drop a SETTLED record from the visible set (caller collected its outcome)."""
        rec = self._owned(job_id, caller)
        if rec.status not in ("completed", "killed", "failed"):
            raise RuntimeError(f"job {job_id} is still live - kill it first")
        # Collected IS a delivery: a settlement nobody gathered would announce forever.
        rec.claimed = True
        rec.notified = True
        self._jobs.pop(job_id, None)
        self._read_locks.pop(job_id, None)

    def live_for_owner(self, owner: str) -> List[str]:
        return [j.id for j in self._jobs.values()
                if j.owner == owner and j.status in ("running", "stopping")]

    async def drain_owner(self, owner: str, reason: str = "owner disposed",
                          timeout: float = 5.0) -> int:
        """Cancel every live job of one owner and await the unwind (stop cascade).

        A producer that ignores cancel_event cannot be forced from here - the registry does
        not own its process. After the grace period such a job is settled as 'killed' with
        cause 'teardown' and a detail that SAYS the producer never acknowledged the cancel,
        so shutdown never hangs on a zombie job and the ledger never claims it finished
        cleanly.
        """
        ids = self.live_for_owner(owner)
        for job_id in ids:
            # Teardown never claims: nobody received the outcome, so the settlement notice
            # is the only thing that tells the model its background work was cut short.
            self.kill(job_id, owner, reason, claim=False)
        if ids:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not self.live_for_owner(owner):
                    break
                await asyncio.sleep(0.05)
        stuck = self.live_for_owner(owner)
        for job_id in stuck:
            rec = self._jobs.get(job_id)
            if rec is not None:
                await self._settle(rec, JobOutcome(
                    "killed",
                    detail=f"producer did not acknowledge cancellation within {timeout}s",
                    cause="teardown"))
        # Announcements are deferred one loop step; a cascade-stop caller (dispose /
        # _cascade_stop) must not return before its notices delivered, or a job nobody read
        # would settle silently. The ledger rows are deferred the same way and awaited here,
        # so a Stop never returns with a job's row still unwritten.
        await self._await_pending(self._announce_tasks)
        await self._await_pending(self._record_tasks)
        return len(ids)

    async def _await_pending(self, bucket: set) -> None:
        """Drain deferred settlement work (notices or rows) - they never block a settle."""
        while bucket:
            await asyncio.gather(*list(bucket), return_exceptions=True)

    async def _await_announcements(self) -> None:
        """Drain pending settlement notices (kept for callers that only care about notices)."""
        await self._await_pending(self._announce_tasks)

    async def close(self) -> None:
        """Process teardown: stop everything, no zombie children or pumps."""
        self._closed = True
        for owner in {j.owner for j in self._jobs.values() if j.owner}:
            await self.drain_owner(owner, "runtime shutdown")
        # Deferred work must not be dropped mid-flight, or a settlement the model never
        # received (and a ledger row never written) would be lost at shutdown.
        await self._await_pending(self._announce_tasks)
        await self._await_pending(self._record_tasks)
        # A job that never settled still has an open spill handle; close it so the file
        # is not left locked (Windows) and the complete stream is flushed.
        for rec in self._jobs.values():
            if rec.spill_close is not None:
                try:
                    rec.spill_close()
                except Exception:
                    pass
                rec.spill_close = None

    # --- the seam a SERVER-side tool uses (2026-12 python_exec run_in_background) ---

    def start_on_loop(self, spec: JobSpec, loop: Optional[asyncio.AbstractEventLoop]) -> str:
        """Register a job from a NON-loop thread (the python_exec worker thread, 2026-11-30).

        `JobRegistry.starter()` is built on the event-loop thread, but the callable it
        hands to the tool is INVOKED on the worker thread that asyncio.to_thread() put
        python_exec on - that thread has no running loop, so start()'s
        asyncio.get_running_loop() / create_task() raised
        'RuntimeError: no running event loop' and run_in_background never worked from
        the tool path (only from tests, which call it inside asyncio.run()).

        Registration is marshalled onto the loop with call_soon_threadsafe and awaited
        on a threading.Event, so the id still comes back synchronously to the caller and
        the pump/watcher tasks are created on the right loop. A thread that has no loop
        at all gets an honest error, never a fake job id.
        """
        if loop is None or loop.is_closed():
            raise RuntimeError("no event loop available for background jobs in this process")
        # Already ON that loop? Then marshalling would DEADLOCK: call_soon_threadsafe
        # queues the callback on the loop, and this very thread is the loop thread that is
        # blocked in done.wait() - the callback could never run. Register directly.
        try:
            if asyncio.get_running_loop() is loop:
                return self.start(spec)
        except RuntimeError:
            pass
        box: Dict[str, Any] = {}
        done = threading.Event()

        def _register() -> None:
            try:
                box["id"] = self.start(spec)
            except BaseException as exc:  # propagate to the calling thread verbatim
                box["err"] = exc
            finally:
                done.set()

        loop.call_soon_threadsafe(_register)
        if not done.wait(10.0):
            raise RuntimeError("job registration timed out on the event loop")
        if "err" in box:
            raise box["err"]
        return box["id"]

    def starter(self):
        """Build the `job_starter` callable handed to a tool through the call context.

        A delivered tool runs inside the loader sandbox on a worker thread and cannot
        import CLIENT modules, so it never touches JobRegistry/JobHooks directly. It gets
        ONE function:

            job_starter(kind, label, owner, spawn_fn, output_limit_bytes=0) -> job_id

        `spawn_fn(handle, cancel_event)` is a BLOCKING sync function the tool owns: it does
        the work, calls handle.append(text, channel) as output arrives, watches
        cancel_event, and returns (status, detail, result). The registry owns the lifecycle,
        the ring, the cursor and the settlement notice — the tool owns the process.
        """
        import threading

        def start(kind: str, label: str, owner: Optional[str],
                  spawn_fn: Callable[[Any, Any], tuple],
                  output_limit_bytes: int = 0,
                  loop: Optional[asyncio.AbstractEventLoop] = None) -> str:
            if self._closed:
                raise RuntimeError("job registry is closed")

            def run(handle):
                cancel = threading.Event()
                # start() runs on the registry's loop (directly, or marshalled by
                # start_on_loop), so the future and watcher land on the right loop.
                home = asyncio.get_running_loop()
                fut = home.create_future()

                def _worker():
                    try:
                        status, detail, result = spawn_fn(handle, cancel)
                    except Exception as exc:  # a broken producer is a FAILED job, never a crash
                        status, detail, result = "failed", f"{type(exc).__name__}: {exc}", ""
                    if status not in ("completed", "killed", "failed"):
                        status = "failed"
                    outcome = JobOutcome(status, str(detail or ""), str(result or ""))
                    # THREAD-SAFE completion. Future.set_result() from a foreign thread is
                    # NOT safe: it appends callbacks without waking the loop, so a job that
                    # settles while the loop is idle is never noticed (it only appeared to
                    # work because other timers happened to wake the loop and drain the
                    # ready queue). call_soon_threadsafe wakes it deliberately.
                    try:
                        home.call_soon_threadsafe(
                            lambda: None if fut.done() else fut.set_result(outcome))
                    except RuntimeError:
                        logger.warning(f"[JOBS] producer finished but the loop is gone ({handle.id})")

                threading.Thread(target=_worker, daemon=True,
                                 name=f"job-{handle.id}").start()

                def _cancel(reason):
                    cancel.set()

                return JobHooks(_cancel, fut)

            spec = JobSpec(kind=kind, label=label, owner=owner, run=run,
                           output_limit_bytes=output_limit_bytes)
            # The tool callable is invoked on python_exec's worker thread (asyncio.to_thread),
            # which has no running loop - register on a loop that DOES exist. The caller's
            # captured loop wins; otherwise fall back to the loop the registry was built on
            # (the server loop), so a caller that never captured a loop still gets a real
            # job instead of 'no running event loop'.
            target = loop if loop is not None else self._home_loop
            if target is not None:
                return self.start_on_loop(spec, target)
            return self.start(spec)

        return start

    # --- producer plumbing -----------------------------------------------

    def _schedule(self, coro_factory, *args, bucket: Optional[set] = None) -> None:
        """Run a registry-side coroutine on the registry's HOME loop, from any thread.

        Producers append output from their own worker thread, and the tool seam is invoked
        on python_exec's worker thread too. A bare asyncio.create_task() there raises
        'no running event loop' - for the output observer that used to be swallowed by a
        bare except, so live UI frames silently vanished. Marshalling onto the home loop
        makes both paths work; a registry with no home loop does nothing rather than throw
        at a producer.

        `bucket` keeps the scheduled task referenced: asyncio holds NO strong reference to
        a task, so a suspended one can be garbage-collected mid-flight. Deferred settlement
        work uses its own bucket (awaitable at teardown); observers use the transient one.
        """
        bucket = self._transient_tasks if bucket is None else bucket
        loop = self._home_loop
        if loop is None or loop.is_closed():
            try:
                task = asyncio.create_task(coro_factory(*args))
                if bucket is not None:
                    bucket.add(task)
                    task.add_done_callback(bucket.discard)
            except RuntimeError:
                logger.debug("[JOBS] no loop available - scheduled work skipped")
            return
        # Am I already ON the home loop? get_running_loop() raises on a foreign thread, so
        # that check needs its own try - otherwise the producer-thread path fell into the
        # except and the observer never fired (the exact bug this replaces).
        on_home = False
        try:
            on_home = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_home = False
        try:
            if on_home:
                task = asyncio.create_task(coro_factory(*args))
                if bucket is not None:
                    bucket.add(task)
                    task.add_done_callback(bucket.discard)
            else:
                asyncio.run_coroutine_threadsafe(coro_factory(*args), loop)
        except RuntimeError:
            logger.debug("[JOBS] scheduling failed (loop state changed)")

    def _append(self, job_id: str, text: str, channel: str, gap_before: bool) -> None:
        rec = self._jobs.get(job_id)
        if rec is None:
            return  # after settlement writes log and drop (never throw at a producer)
        rec.ring.append(text, channel, gap_before)
        if self._on_output is not None and channel != "log":
            self._schedule(lambda: self._on_output(rec, text, channel))

    def _set_progress(self, job_id: str, line: str) -> None:
        rec = self._jobs.get(job_id)
        if rec is not None:
            rec.progress = line

    async def _pump(self, rec: JobRecord) -> None:
        """Pull sources at a fixed cadence; drain ONE last time before settlement.

        A source reports (text, nextOffset, lossy, spillPath): offsets are whole-stream
        positions that never move, so a source that keeps its own spill file can hand the
        registry a path for bytes the ring already evicted.
        """
        sources = rec.sources
        offsets = [0] * len(sources)
        try:
            while rec.status in ("running", "stopping"):
                for i, src in enumerate(sources):
                    try:
                        read = src.read(offsets[i]) or {}
                    except Exception:
                        continue
                    if read.get("text"):
                        rec.ring.append(read["text"], read.get("channel") or "stdout",
                                         bool(read.get("lossy")))
                    offsets[i] = int(read.get("nextOffset", offsets[i]))
                    if read.get("spillPath"):
                        rec.ring._spill_path = read["spillPath"]  # noqa: SL001 - metadata only
                await asyncio.sleep(self._pump_poll)
        except asyncio.CancelledError:
            pass
        finally:
            # Final drain: the last bytes a producer wrote just before exiting.
            for i, src in enumerate(sources):
                try:
                    read = src.read(offsets[i]) or {}
                    if read.get("text"):
                        rec.ring.append(read["text"], read.get("channel") or "stdout",
                                         bool(read.get("lossy")))
                except Exception:
                    pass

    async def _watch(self, rec: JobRecord) -> None:
        """Await the producer's done; convert a rejection to 'failed'. Must never raise."""
        try:
            outcome = await rec.hooks.done  # type: ignore[assignment]
            if not isinstance(outcome, JobOutcome):
                outcome = JobOutcome("completed", result=str(outcome or ""))
            await self._settle(rec, outcome)
        except asyncio.CancelledError:
            await self._settle(rec, JobOutcome("killed", detail="runtime teardown",
                                               cause="teardown"))
            raise
        except Exception as exc:
            await self._settle(rec, JobOutcome("failed", detail=f"{type(exc).__name__}: {exc}"))

    async def _settle(self, rec: JobRecord, outcome: JobOutcome) -> None:
        """FIRST-WINS settlement: one terminal record, released waiters, one notice."""
        if rec.status in ("completed", "killed", "failed"):
            return
        rec.status = outcome.status
        # WHO ended it: the producer's own report, a kill the agent asked for, or runtime
        # teardown. A shutdown must never read as a producer failure, and a kill must keep
        # the reason the agent gave.
        if outcome.cause:
            rec.cause = outcome.cause
        elif rec.status in ("completed", "failed") and not rec.cause:
            rec.cause = "producer"
        detail = outcome.detail or ""
        if rec.cause == "kill" and detail:
            rec.detail = f"{detail} ({rec.detail})" if rec.detail else detail
        else:
            rec.detail = detail or rec.detail
        rec.result = outcome.result
        rec.producer_reported = True
        rec.finished_at = time.time()
        rec.progress = ""
        # Settled retention keeps every byte the model has NOT consumed yet, on top of the
        # settled cap. Trimming to the bare cap used to evict unread output at settlement,
        # so the notice (which reads from the cursor) reported a lossy/partial stream for a
        # job the model never read - the spill file was the only complete copy left.
        rec.ring.shrink(max(self._settled_retain, rec.ring.total - rec.cursor))
        rec.ring.close()

        pump = rec.pump_task
        if pump is not None and not pump.done():
            pump.cancel()
        for fut in rec.waiters:
            if not fut.done():
                fut.set_result(rec.view())
        rec.waiters.clear()

        # The spill file is the complete stream, so it must be written by every chunk and
        # closed once at settlement (see _spill_path_for).
        if rec.spill_close is not None:
            try:
                rec.spill_close()
            except Exception:
                pass
            rec.spill_close = None

        logger.info(f"[JOBS] settled {rec.id} status={rec.status} detail={rec.detail[:120]}")
        # The ledger row + UI row fire on EVERY settlement, claimed or not. Deferred one
        # loop step like the notice, so the row carries the final state (a kill settles
        # after the tool result already answered).
        if self._on_record is not None and not rec.row_recorded:
            rec.row_recorded = True
            self._schedule(self._record_row, rec, bucket=self._record_tasks)
        # A claimed job was already delivered to the model by a tool result - no notice.
        # The announcement is DEFERRED to a task, not awaited here: a waiter released just
        # above resumes on the next loop step and claims the job first. Deciding inline
        # would fire the notice for a job whose outcome the model already received through
        # job_output(wait=true) - a duplicate delivery of the same settlement.
        if self._on_settle is not None and not rec.notified and not rec.claimed:
            rec.notified = True
            self._schedule(self._announce, rec, bucket=self._announce_tasks)

    async def _record_row(self, rec: JobRecord) -> None:
        """Write the durable ledger row + UI row for one settlement."""
        try:
            outcome = self._on_record(rec)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # a row must never break a settlement
            logger.debug(f"[JOBS] ledger record failed for {rec.id}: {exc!r}")

    async def _announce(self, rec: JobRecord) -> None:
        """Fire the settlement notice, re-checking the claim at run time."""
        if rec.claimed:
            return  # a waiter claimed it in this tick - the tool result already delivered it
        try:
            outcome = self._on_settle(rec)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # a notice must never break a settlement
            logger.debug(f"[JOBS] settlement notice failed for {rec.id}: {exc!r}")


# --- module singleton ---------------------------------------------------

_registry: Optional[JobRegistry] = None


def get_job_registry() -> Optional[JobRegistry]:
    return _registry


def set_job_registry(registry: JobRegistry) -> None:
    global _registry
    _registry = registry


def create_job_registry(spill_dir: str) -> JobRegistry:
    """Build + install the process registry (called once at app startup)."""
    registry = JobRegistry(spill_dir=spill_dir)
    set_job_registry(registry)
    return registry
