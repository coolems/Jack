"""ChildRegistry — delegated subagents (client side, 2026-12).

One seam, two in-process modes:

  * spawn  — a FRESH child: system prompt + the task only. The task must stand alone,
    because the child sees nothing of the parent's conversation.
  * fork   — the parent's COMPLETED turns only, never the in-flight turn. The child can
    build on the conversation without the parent paying for the child's work in its own
    context.

The tool returns the child id IMMEDIATELY. Execution is owned here; the result arrives
later as a runtime notice on the parent channel (busy parent → injected at its next safe
boundary; idle parent → a follow-up turn, budgeted).

Rules that keep this safe on one machine:
  * depth cap (default 1): children may not delegate. Enforced at admission.
  * concurrency cap per parent conversation.
  * CHILD_FORBIDDEN_TOOLS: the physical screen / shared browser / image generation /
    delegation. Two loops driving one mouse produce garbage, and the singleton tool_lock
    would serialise them anyway. Enforced HERE (schema) and fail-closed in ToolExecutor.
  * a child-level failure RESOLVES (stop_reason='error') — it never rejects, so a broken
    child cannot kill the parent's turn.
  * stop cascades child-first and is awaited.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from config import (
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_MAX_CONCURRENT_PER_CONV,
    SUBAGENT_TIMEOUT_SEC,
)

logger = logging.getLogger("COOLEMS.Runtime.Children")

#: Tools a child may never call. Screen/browser are singleton resources; delegation is
#: the depth budget; image generation is a GPU resource the parent should own.
CHILD_FORBIDDEN_TOOLS: frozenset = frozenset({
    # computer_use (physical desktop)
    "cua_capabilities", "cua_click", "cua_drag", "cua_get_app", "cua_get_state",
    "cua_hover", "cua_list_apps", "cua_list_windows", "cua_paste", "cua_perform_action",
    "cua_press_key", "cua_screenshot", "cua_scroll", "cua_select_text", "cua_show_window",
    "cua_type_text",
    # shared browser
    "browser_screenshot", "check_url", "connect", "disconnect", "download_file",
    "ensure_connected", "get_page_content", "go_back", "goto", "reload",
    "save_current_image", "scroll",
    # GPU / delegation / parent-only control surfaces
    "generate_image", "subagent", "subagent_fork", "send_message", "interrupt_agent",
    "list_agents", "list_subagent_models", "ask_user_question",
})

MODES = ("spawn", "fork")

#: (2026-12) How long an interrupted child gets to unwind before its aborted report is
#: written. A cancelled task unwinds in one step (its finally captures the tool results);
#: a transport that shields cancellation (the relay WS call) keeps running until the
#: provider answers, so the grace has to cover an in-flight request - otherwise the report
#: is written while the child's loop is still mid-call and comes out empty (observed live:
#: steps 4 / tool calls 4, report "(the child produced no text)"). Bounded so a stuck
#: producer never hangs the settlement or the fan-out slot forever.
ABORT_UNWIND_SEC = 8.0
#: (2026-12) After the report is written, how long a still-alive child task may keep
#: producing findings that get folded into the record afterwards (late capture).
LATE_CAPTURE_SEC = 30.0


def _observed_tool_tail(rec: "ChildRecord") -> List[str]:
    """The child's tool results, from whichever source is live right now.

    (2026-12) tool_tail is written in run_child's finally, which only runs when the child's
    task unwinds - so an interrupt that lands inside a long provider/tool call settles with
    an EMPTY tool_tail (observed live: a child that had already run two greps reported only
    its prose). rec.messages is the child's OWN live list, so reading it here gives the
    results gathered up to the moment of the cut-off, whatever the unwind does.
    """
    tail = [t for t in (rec.tool_tail or []) if t.strip()]
    if tail:
        return tail
    msgs = rec.messages or []
    return [str(m.get("content") or "")[:400]
            for m in msgs if isinstance(m, dict) and m.get("role") == "tool"][-6:]


def observed_work(rec: "ChildRecord") -> tuple:
    """(steps, tool_calls) - the record counters PLUS whatever its live messages show.

    (2026-12) Both counters are written in run_child's finally, which only runs when the
    child's task unwinds. A cancel that lands inside a long provider/tool call skips it, so
    a child that had already run several greps settled reading "steps: 0, tool calls: 0" -
    the parent judged the work as not done. rec.messages is the child's OWN live list, so
    anything appended past what the finally already counted is added here.
    """
    msgs = [m for m in (rec.messages or []) if isinstance(m, dict)]
    # Only messages the child's OWN loop appended count: a fork seed or a continuation
    # carries past assistant turns, and those are already in the counters.
    start = max(int(getattr(rec, "counted_upto", 0) or 0),
                int(getattr(rec, "seed_len", 0) or 0))
    tail = msgs[start:]
    steps = int(rec.steps or 0) + sum(1 for m in tail if m.get("role") == "assistant")
    tools = int(rec.tool_calls or 0)
    total_tools = sum(1 for m in msgs if m.get("role") == "tool")
    return steps, max(tools, total_tools)


def partial_report(rec: "ChildRecord") -> str:
    """What a capped / interrupted / failed child still has, in priority order.

    (2026-11-30) A delegated child does its work in TOOL RESULTS, not in streamed prose.
    The old code reported only `rec.partial` (the streamed text), so a child cut off by
    the wall-clock cap after 31 tool calls came back as one opening sentence - the
    delegated work was lost even though the child had read dozens of files. The child's
    own messages survive cancellation, so its last tool results ride along.
    """
    parts: List[str] = []
    prose = (rec.partial or "").strip()
    if prose:
        parts.append(prose)
    tail = _observed_tool_tail(rec)
    if tail:
        parts.append("--- what the child had already gathered (its last tool results) ---\n"
                     + "\n\n".join(tail))
    return "\n\n".join(parts)


class ChildSteerSource:
    """Steering face for ONE running child (same contract as ChatChannel's steer queue).

    `send_message` on a running child is steering: the child's loop injects it at its
    next safe boundary. It is NOT a new turn and NOT a stop.
    """

    def __init__(self) -> None:
        self._queue: List[Dict] = []
        self._applied: set = set()

    def queue(self, text: str) -> Dict:
        item = {"id": uuid.uuid4().hex, "text": text, "received_at": time.time()}
        self._queue.append(item)
        return item

    def has_unapplied_steer(self) -> bool:
        return any(i["id"] not in self._applied for i in self._queue)

    def drain_steer(self) -> List[Dict]:
        pending = [i for i in self._queue if i["id"] not in self._applied]
        self._queue.clear()
        return pending

    def mark_steer_applied(self, steer_id: Optional[str]) -> None:
        if steer_id:
            self._applied.add(str(steer_id))


class ChildSink:
    """Provider-facing socket for a child: frames ride on the PARENT channel, tagged.

    (2026-11-30 fix) A child's frames used to be forwarded to the parent UI AS-IS, so:
      * its streamed content/thinking chunks arrived as `content` frames with no turn_id
        -> the UI put them in the PARENT's open block answer strip (the parent's answer
        showed the child's words, and the child's `done` frame settled the parent's block
        and cleared the parent's streaming state while the parent was still working);
      * its tool rows and token stats polluted the parent's turn block.
    So the sink now forwards ONLY what the parent's UI/user genuinely needs:
      * content/thinking  -> a `cycle_child_output` frame tagged with child_id, painted as
        the child's own output lines inside that child's row;
      * consent + question frames -> forwarded, because consent belongs to the HUMAN;
      * everything else (tool rows, stats, done, queue frames) -> dropped.
    A dead parent socket must never break a child.
    """

    #: Frames a child may show to the human: consent cards and question cards.
    FORWARD_TYPES = frozenset({"exec_approval_request", "cycle_consent",
                               "cycle_consent_pending", "cycle_question",
                               "cycle_question_pending", "grant"})

    def __init__(self, channel, child_id: str, on_text: Callable[[str], None] = None):
        self._channel = channel
        self.child_id = child_id
        self.captured_text = ""
        self.captured_thinking = ""
        self._on_text = on_text
        # The turn that launched this child: its id keeps the child's output rows in the
        # block that delegated it, even after that turn has ended.
        self.launch_turn_id = str(getattr(channel, "turn_id", "") or "")

    async def send_text(self, data: str) -> None:
        try:
            import json as _json
            msg = _json.loads(data)
        except (ValueError, TypeError):
            msg = None
        if not isinstance(msg, dict):
            return   # opaque payload: never forwarded raw into the parent's stream
        t = msg.get("type")
        chunk = msg.get("content")
        if t in ("content", "thinking") and isinstance(chunk, str) and chunk:
            if t == "content":
                self.captured_text += chunk
                if self._on_text is not None:
                    try:
                        self._on_text(chunk)
                    except Exception:
                        pass
            else:
                self.captured_thinking += chunk
            try:
                await self._channel.emit_frame({
                    "type": "cycle_child_output", "conv_id": getattr(self._channel, "conv_id", ""),
                    "turn_id": self.launch_turn_id, "child_id": self.child_id,
                    "text": chunk[:600], "channel": "thinking" if t == "thinking" else "answer",
                    "at_ts": None,
                })
            except Exception:
                pass
            return
        if t in self.FORWARD_TYPES:
            msg["child_id"] = self.child_id
            msg["conv_id"] = getattr(self._channel, "conv_id", "")
            try:
                await self._channel.emit_raw(_json.dumps(msg))
            except Exception:
                pass
            return
        # Anything else (tool rows, token stats, done, queue frames) belongs to the child's
        # own loop, not to the parent's UI.
        return

    def __getattr__(self, name):
        subs = getattr(self._channel, "subscribers", set())
        if subs:
            return getattr(next(iter(subs)), name)

        async def _noop(*args, **kwargs):
            return None

        return _noop


class ChildRecord:
    """One delegated child: identity, lineage, live state, result."""

    def __init__(self, *, child_id: str, parent_conv_id: str, mode: str, depth: int,
                 label: str, task: str, owner_conv_id: str = ""):
        self.id = child_id
        # LINEAGE: who delegated this child (a chat id, or another child's id).
        self.parent_conv_id = parent_conv_id
        # OWNER CHAT: the conversation that receives the notice, the ledger row and the UI
        # frames. A grandchild's notices must land in the ROOT chat, not in a channel named
        # after its parent child (which would create a phantom channel on the bus).
        self.owner_conv_id = owner_conv_id or parent_conv_id
        self.mode = mode
        self.depth = depth
        self.label = label
        self.task = task
        self.status = "running"          # running | settled | aborted | error
        # completed | aborted | error | wall-clock (the child's own cap, not a token cap)
        self.stop_reason = ""
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.output = ""
        # (2026-11-30) Text streamed so far. A child cut off by the cap still has real
        # findings; settling with output="" used to report "(the child produced no text)".
        self.partial = ""
        self.diagnostic = ""
        self.turns = 0
        # (2026-11-30) Provider steps of the child's own loop. `turns` counts run_child
        # CALLS (1 per delegation/continuation), so a child that did 31 tool calls reported
        # "turns: 1" - the parent read that as the child having barely worked. steps is the
        # honest count of provider iterations inside that turn.
        self.steps = 0
        self.tool_calls = 0
        # (2026-12) Where run_child's finally counted up to in rec.messages, and how long
        # this turn's seed was. observed_work() uses them to count the child's real work
        # from its LIVE message list when the finally never ran (cancel inside a long call).
        self.seed_len = 0
        self.counted_upto = 0
        # (2026-11-30) The child's last tool results, kept so a child cut off by the cap
        # still reports its FINDINGS. A delegated child does its work in tool results, not
        # in streamed prose: a capped child used to report only its opening sentence and
        # everything it had read was lost. Bounded (few results, few chars each).
        self.tool_tail: List[str] = []
        self.stop_event = asyncio.Event()
        # (2026-12) Set the moment a stop is requested. The slot accounting excludes a
        # stop-requested child IMMEDIATELY: the aborted report waits for the child's task
        # to unwind (a relay WS call cannot be cancelled mid-flight), and holding the
        # fan-out slot for that grace period would block a delegation the user just asked
        # for. The status stays 'running' until the record actually settles.
        self.stop_requested = False
        self.steers = ChildSteerSource()
        self.task_handle: Optional[asyncio.Task] = None
        # (2026-12) The child's OWN turn task, separate from the registry wrapper task.
        # Cancelling only the wrapper skips run_child's finally (the tool-result capture),
        # so a stop must cancel THIS task and await its unwind.
        self.run_handle: Optional[asyncio.Task] = None
        # (2026-11-30) The task of a CONTINUATION turn. start_continuation() awaits the
        # executor directly, so the original task_handle is already done - without this,
        # interrupt()/drain_children() could not stop or await a continuation in flight.
        self.continuation_handle: Optional[asyncio.Task] = None
        # (2026-12) The settle task interrupt() scheduled for this record, so a stop
        # cascade can AWAIT it (a record must not stay 'running' after its owner stopped).
        self.abort_settle: Optional[asyncio.Task] = None
        # (2026-12) The late-capture watcher: a child whose provider call shields
        # cancellation keeps producing findings after the aborted report was written, and
        # those get folded into the record. Cancelled on close so it never outlives shutdown.
        self.late_capture: Optional[asyncio.Task] = None
        self.messages: List[Dict] = []   # the child's own conversation (for continuation)
        self.notified = False            # exactly one settlement notice per child
        self.continuations = 0

    def view(self) -> Dict:
        # (2026-12) steps/tool_calls come from observed_work(), so a child cut off inside a
        # long provider/tool call still shows the work it actually did (its counters are
        # written only in run_child's finally, which that cancel skips).
        steps, tool_calls = observed_work(self)
        # A stop-requested child whose report is still waiting on the unwind grace reads
        # "stopping", not "running" - the user pressed Stop, and the record is not settled
        # yet only because an in-flight provider call cannot be cancelled.
        status = "stopping" if (self.status == "running" and self.stop_requested) else self.status
        return {
            "id": self.id,
            "parent_conv_id": self.parent_conv_id,
            "owner_conv_id": self.owner_conv_id,
            "mode": self.mode,
            "depth": self.depth,
            "label": self.label,
            "status": status,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "turns": self.turns,
            "steps": steps,
            "tool_calls": tool_calls,
            "output_chars": len(self.output),
            "continuations": self.continuations,
        }


class ChildRegistry:
    """parent conv_id -> child records, with execution ownership + cascade stop."""

    def __init__(self, *, max_depth: int = SUBAGENT_MAX_DEPTH,
                 max_per_parent: int = SUBAGENT_MAX_CONCURRENT_PER_CONV,
                 timeout_sec: int = SUBAGENT_TIMEOUT_SEC):
        self._children: Dict[str, ChildRecord] = {}
        self._max_depth = max(0, int(max_depth))
        self._max_per_parent = max(1, int(max_per_parent))
        self._timeout = max(0, int(timeout_sec))
        self._on_settle: Optional[Callable[[ChildRecord], Awaitable[None]]] = None
        # (2026-12) Ledger/UI recorder WITHOUT the notice. A CONTINUATION turn is returned
        # to the caller as a tool result, so announcing it again would deliver the same
        # reply twice - but the durable row and the child's UI row must still exist, or a
        # capped or aborted continuation leaves no trace of what the child did.
        self._on_record: Optional[Callable[[ChildRecord], Awaitable[None]]] = None
        self._run_child: Optional[Callable[..., Awaitable[str]]] = None
        self._closed = False

    def wire(self, *, run_child: Callable[..., Awaitable[str]],
             on_settle: Callable[[ChildRecord], Awaitable[None]],
             on_record: Optional[Callable[[ChildRecord], Awaitable[None]]] = None) -> None:
        """Install the executor (child_run), the settlement notice handler, and the
        notice-free ledger/UI recorder used by continuation turns."""
        self._run_child = run_child
        self._on_settle = on_settle
        self._on_record = on_record

    # --- admission -------------------------------------------------------

    def depth_of(self, conv_id: str) -> int:
        """Delegation depth of a conversation: 0 for a root chat, 1 for a child of one.

        The depth is the child's OWN recorded level, not level+1: `assert_admissible`
        compares it against the cap, and the notice/UI text prints it as "depth N".
        """
        rec = self._children.get(conv_id)
        return rec.depth if rec is not None else 0

    def _parent_count(self, parent_conv_id: str) -> int:
        # A child whose stop has been REQUESTED is no longer occupying a slot: its report
        # may still be waiting on an uncancellable provider call, but the user (or the
        # model) has already decided that work is over - see ChildRecord.stop_requested.
        return sum(1 for c in self._children.values()
                   if c.parent_conv_id == parent_conv_id
                   and c.status == "running" and not c.stop_requested)

    def assert_admissible(self, parent_conv_id: str) -> None:
        """Fail loud BEFORE starting work: depth budget + concurrency budget.

        The message must read as a POLICY limit, not as a broken agent: it names the chat
        (never a bare uuid, which looks like a failing agent id), the live count, and the
        two ways out (wait for one, or interrupt one).
        """
        if self._closed:
            raise RuntimeError("child registry is closed")
        # A conversation AT the cap may not delegate: max_depth=1 means one level of
        # delegation (a root chat may delegate; its child may not).
        if self.depth_of(parent_conv_id) >= self._max_depth:
            raise RuntimeError(
                f"delegation depth limit reached ({self._max_depth}). Children of a "
                "child may not delegate - do the work yourself or ask the user.")
        live = self._parent_count(parent_conv_id)
        if live >= self._max_per_parent:
            raise RuntimeError(
                f"this chat already has {live}/{self._max_per_parent} live children - "
                "the fan-out budget is spent. Call list_agents to see what is still "
                "running, wait for one to settle, or interrupt_agent one of them before "
                "delegating again.")

    def capacity(self, parent_conv_id: str) -> Dict[str, int]:
        """Live/total delegation slots for one chat. Cheap pre-flight the tool can print."""
        return {"live": self._parent_count(parent_conv_id),
                "max": self._max_per_parent,
                "free": max(0, self._max_per_parent - self._parent_count(parent_conv_id))}

    def start(self, *, parent_conv_id: str, mode: str, task: str, label: str = "",
              context: Dict) -> str:
        """Register + start a child. Returns the child id immediately (never awaits)."""
        mode = mode if mode in MODES else "spawn"
        self.assert_admissible(parent_conv_id)
        child_id = f"child-{uuid.uuid4().hex[:8]}"
        # OWNER CHAT: walk up the lineage so a grandchild's notice/ledger/UI row lands in
        # the ROOT chat. Delivering to parent_conv_id would wake a channel named after a
        # child id (a phantom channel on the bus) and the root chat would never learn.
        owner = parent_conv_id
        seen: set = set()
        while owner in self._children and owner not in seen:
            seen.add(owner)
            owner = self._children[owner].owner_conv_id
        rec = ChildRecord(child_id=child_id, parent_conv_id=parent_conv_id, mode=mode,
                          # The child's own level: a child of a root chat is depth 1.
                          # assert_admissible() already refused anything at the cap.
                          depth=self.depth_of(parent_conv_id) + 1,
                          label=label or task[:80], task=task, owner_conv_id=owner)
        self._children[child_id] = rec
        rec.task_handle = asyncio.create_task(self._execute(rec, context),
                                              name=f"child-{child_id}")
        logger.info(f"[SUBAGENT] started {child_id} mode={mode} depth={rec.depth} "
                    f"parent={parent_conv_id[:8]} label={rec.label[:60]}")
        return child_id

    async def _execute(self, rec: ChildRecord, context: Dict) -> None:
        """Run one child turn; settle EXACTLY once, whatever happens."""
        if self._run_child is None:  # pragma: no cover - wiring bug, fail loud
            await self._settle(rec, "", "error", "child executor not wired")
            return
        # (2026-12) The child's turn is its OWN task, held on the record. Cancelling THIS
        # outer task alone skips run_child's finally (wait_for re-raises without awaiting
        # the inner unwind), so the report snapshot was taken before the tool results were
        # captured - an interrupted child reported "(the child produced no text)".
        inner = asyncio.ensure_future(self._run_child(rec, context))
        rec.run_handle = inner
        try:
            if self._timeout > 0:
                output = await asyncio.wait_for(inner, timeout=self._timeout)
                stop_reason = "aborted" if rec.stop_event.is_set() else "completed"
                await self._settle(rec, output, stop_reason, "")
            else:
                output = await inner
                await self._settle(rec, output,
                                   "aborted" if rec.stop_event.is_set() else "completed", "")
        except asyncio.TimeoutError:
            # (2026-11-30) Two fixes here. The reason was 'max-tokens', which is a lie -
            # this cap is WALL CLOCK, and the parent read it as a token limit. And output
            # was "" even though the child had streamed real findings, so the notice said
            # "(the child produced no text)" and the delegated work was lost.
            await self._await_unwind(inner)
            await self._settle(rec, partial_report(rec), "wall-clock",
                               f"child wall-clock cap {self._timeout}s exceeded - "
                               "the report below is PARTIAL (cut off at the cap)")
            # (2026-12) A transport that shields cancellation keeps producing after the
            # cap cut it off; fold those findings into the record like an interrupt does.
            self._watch_late_capture(rec, [inner])
        except asyncio.CancelledError:
            # Give the child's own task its unwind first (bounded): its finally is what
            # captures the tool results. If it never gets there, partial_report() still
            # reads rec.messages directly.
            await self._await_unwind(inner)
            await self._settle(rec, partial_report(rec), "aborted", "interrupted")
            # (2026-12) This branch can win the first-wins race against _abort_settle()
            # (the cancel propagates here before that task settles), so the late-capture
            # watcher has to be scheduled here too - _watch_late_capture() is idempotent.
            self._watch_late_capture(rec, [inner])
            raise
        except Exception as exc:
            # A child-level failure RESOLVES. The parent gets an isError tool result /
            # notice, never a crashed turn.
            logger.warning(f"[SUBAGENT] child {rec.id} failed: {exc!r}")
            await self._await_unwind(inner)
            await self._settle(rec, partial_report(rec), "error", f"{type(exc).__name__}: {exc}")
        finally:
            rec.run_handle = None

    async def _await_unwind(self, inner: Optional[asyncio.Task]) -> None:
        """Let a cancelled/capped child task finish its own finally, bounded.

        Never swallows the caller's cancellation: asyncio.wait reports the observed task's
        state instead of re-raising it, so this is safe inside an except-CancelledError path.
        """
        if inner is None or inner.done():
            return
        try:
            await asyncio.wait({inner}, timeout=ABORT_UNWIND_SEC)
        except Exception:
            pass  # a stuck child must never hang a settlement

    async def _settle(self, rec: ChildRecord, output: str, stop_reason: str,
                      diagnostic: str) -> None:
        if rec.status != "running":
            return
        rec.status = "aborted" if stop_reason == "aborted" else (
            "error" if stop_reason == "error" else "settled")
        rec.stop_reason = stop_reason
        rec.output = output or ""
        rec.diagnostic = (diagnostic or "")[:4096]   # bounded; never tool input or secrets
        rec.finished_at = time.time()
        logger.info(f"[SUBAGENT] settled {rec.id} reason={rec.stop_reason} "
                    f"output_chars={len(rec.output)}")
        if self._on_settle is not None and not rec.notified:
            rec.notified = True
            try:
                await self._on_settle(rec)
            except Exception as exc:  # a notice must never break a settlement
                logger.debug(f"[SUBAGENT] settlement notice failed for {rec.id}: {exc!r}")

    # --- control ---------------------------------------------------------

    def _owned(self, child_id: str, caller: Optional[str]) -> ChildRecord:
        """Owner fence: a chat may only address work ITS OWN lineage started.

        (2026-11-30) Fenced on owner_conv_id, not parent_conv_id: a grandchild's parent is
        a CHILD id, so fencing on the parent made the root chat unable to steer or stop its
        own descendant (PermissionError on work it delegated). The owner chain is the real
        ownership; another chat still gets PermissionError.
        """
        rec = self._children.get(child_id)
        if rec is None:
            raise KeyError(f"unknown child id: {child_id}")
        if caller and rec.owner_conv_id != caller and rec.parent_conv_id != caller:
            raise PermissionError(f"child {child_id} belongs to another conversation")
        return rec

    def get(self, child_id: str, caller: Optional[str] = None) -> Dict:
        return self._owned(child_id, caller).view()

    def list_children(self, caller: str, scope: str = "children") -> List[Dict]:
        """scope='children' → direct children of caller; 'descendants' → the whole subtree."""
        if scope == "descendants":
            out: List[Dict] = []
            frontier = [caller]
            seen: set = set()
            while frontier:
                cid = frontier.pop()
                for rec in self._children.values():
                    if rec.parent_conv_id == cid and rec.id not in seen:
                        seen.add(rec.id)
                        out.append(rec.view())
                        frontier.append(rec.id)
            return out
        return [rec.view() for rec in self._children.values()
                if rec.parent_conv_id == caller]

    def live_children(self, parent_conv_id: str) -> List[str]:
        return [c.id for c in self._children.values()
                if c.parent_conv_id == parent_conv_id and c.status == "running"]

    def send_message(self, child_id: str, text: str, caller: Optional[str] = None) -> Dict:
        """Steer a RUNNING child, or continue a SETTLED one's conversation.

        Running → queued as a steering message for the child's next safe boundary.
        Settled → the caller must start a continuation turn (start_continuation); this
        returns the instruction so the tool result tells the model what happened.
        """
        rec = self._owned(child_id, caller)
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reason": "empty message"}
        if rec.status == "running":
            item = rec.steers.queue(text)
            return {"ok": True, "mode": "steering", "child_id": child_id,
                    "steer_id": item["id"],
                    "note": "Queued for the child's next step. It is still running."}
        if rec.status == "error":
            return {"ok": False, "mode": "error", "child_id": child_id,
                    "reason": rec.diagnostic or "the child failed and cannot be continued"}
        return {"ok": True, "mode": "continuation", "child_id": child_id,
                "note": "The child already finished. Its conversation is kept - a "
                        "continuation turn will be started."}

    async def start_continuation(self, child_id: str, text: str,
                                 caller: Optional[str] = None,
                                 context: Optional[Dict] = None) -> str:
        """Continue a settled child's conversation with one more turn. Returns the reply.

        (2026-11-30 fix) A continuation used to run with NONE of the guarantees the first
        turn has: no wall-clock cap, no stop_event honoured, and a CancelledError was
        swallowed into status='settled'. So a Stop during a continuation kept the child
        running (its task was not even tracked), and a cancelled continuation reported as
        a clean finish. It now runs as a tracked task under the SAME cap, and settles with
        the honest reason.
        """
        rec = self._owned(child_id, caller)
        if rec.status == "running":
            raise RuntimeError(f"child {child_id} is still running - use steering")
        if self._run_child is None:
            raise RuntimeError("child executor not wired")
        rec.continuations += 1
        rec.status = "running"
        rec.stop_reason = ""
        rec.stop_event.clear()
        rec.notified = True  # a continuation is not a new delegation; no extra notice
        # A fresh partial for this turn: the previous turn's text is already in rec.output.
        rec.partial = ""
        rec.tool_tail = []

        inner_box: List[asyncio.Task] = []

        async def _run() -> str:
            # (2026-12) Same shape as _execute: the child's turn is its OWN task, so a
            # cancel reaches run_child's finally (which captures the tool results) before
            # anything is reported. wait_for alone re-raises without awaiting the unwind.
            inner = asyncio.ensure_future(self._run_child(rec, context or {},
                                                          continuation=text))
            rec.run_handle = inner
            inner_box.append(inner)
            try:
                if self._timeout > 0:
                    return await asyncio.wait_for(inner, timeout=self._timeout)
                return await inner
            finally:
                rec.run_handle = None
                if not inner.done():
                    inner.cancel()
                # Let the child's finally finish so the partial report has its findings.
                await self._await_unwind(inner)

        task = asyncio.create_task(_run(), name=f"child-cont-{child_id}")
        rec.continuation_handle = task
        try:
            output = await task
        except asyncio.TimeoutError:
            # (2026-12) Same rule as the first turn: partial_report(), not rec.partial.
            # A continuation clears tool_tail (above), so reading rec.partial alone lost
            # the tool results this turn gathered. status='settled' + stop_reason
            # 'wall-clock' stays honest: the reply IS partial, and the diagnostic says so.
            rec.status = "settled"
            rec.stop_reason = "wall-clock"
            rec.diagnostic = (f"child wall-clock cap {self._timeout}s exceeded on a "
                              "continuation - the reply below is PARTIAL")
            rec.finished_at = time.time()
            output = partial_report(rec)
            rec.output = output or rec.output
            await self._record_continuation(rec)
            # (2026-12) A shielded provider call keeps producing after the cap; fold the
            # late findings into the record (the row above updates again if they arrive).
            self._watch_late_capture(rec, inner_box)
            return rec.output
        except asyncio.CancelledError:
            rec.status = "aborted"
            rec.stop_reason = "aborted"
            rec.diagnostic = "continuation interrupted"
            rec.finished_at = time.time()
            # The caller's await raises (Stop), so nobody receives this reply - the ledger
            # row and UI row are the only trace that the continuation was cut short.
            await self._record_continuation(rec)
            # (2026-12) Same rule as the cap branch above: a shielded provider call keeps
            # producing after the cancel, so its findings must still be folded into the
            # record. Without this the interrupted continuation kept an empty row.
            self._watch_late_capture(rec, inner_box)
            raise
        except Exception as exc:
            rec.status = "error"
            rec.stop_reason = "error"
            rec.diagnostic = f"{type(exc).__name__}: {exc}"[:4096]
            rec.finished_at = time.time()
            await self._record_continuation(rec)
            raise
        finally:
            rec.continuation_handle = None
            if rec.status == "running":
                rec.status = "aborted" if rec.stop_event.is_set() else "settled"
                rec.stop_reason = "aborted" if rec.stop_event.is_set() else "completed"
            rec.finished_at = rec.finished_at or time.time()
        rec.output = output or rec.output
        await self._record_continuation(rec)
        return rec.output

    async def _record_continuation(self, rec: ChildRecord) -> None:
        """Ledger + UI row for a continuation turn, WITHOUT a notice.

        A continuation's reply is handed back to the caller as a tool result, so a
        settlement notice would deliver the same text twice. But a capped or aborted
        continuation must still leave a durable row - otherwise the child's work in that
        turn is invisible after a reload.
        """
        if self._on_record is None:
            return
        try:
            await self._on_record(rec)
        except Exception as exc:  # a UI/ledger failure must never break a continuation
            logger.debug(f"[SUBAGENT] continuation record failed for {rec.id}: {exc!r}")

    def interrupt(self, child_id: str, caller: Optional[str] = None,
                  reason: str = "interrupted by the agent") -> Dict:
        """Stop ONE child (and its descendants). Cooperative: the child's loop polls
        its stop_event at every boundary."""
        rec = self._owned(child_id, caller)
        if rec.status != "running":
            return {"ok": False, "status": rec.status, "reason": "already finished"}
        for desc in self._descendants(child_id):
            desc.stop_event.set()
            desc.stop_requested = True
            self._abort(desc, reason)
        rec.stop_event.set()
        rec.stop_requested = True
        self._abort(rec, reason)
        logger.info(f"[SUBAGENT] interrupted {child_id} ({reason})")
        return {"ok": True, "status": "stopping", "child_id": child_id}

    def _abort(self, rec: ChildRecord, reason: str) -> None:
        """Cancel one child's task AND settle the record.

        Two facts make the settle necessary here:
          * a task cancelled BEFORE its first step never runs its own except-clause (asyncio
            completes it as cancelled without entering the coroutine), so the record would
            stay 'running' forever - it would hold the parent's concurrency budget and its
            settlement notice would never arrive;
          * a task that IS running unwinds through _execute's except-clause and settles
            itself. _settle is FIRST-WINS, so settling here is safe either way: whichever
            path wins writes one terminal record and exactly one notice.

        (2026-11-30) A continuation turn has its own task (continuation_handle); the
        original task_handle is already done, so cancelling only that one would leave a
        running continuation alive after a Stop.
        """
        for handle in (rec.task_handle, rec.continuation_handle):
            if handle is not None and not handle.done():
                handle.cancel()
        if rec.status == "running":
            # partial_report(), not rec.output: the child's streamed text and its last tool
            # results are what the parent needs. _settle is FIRST-WINS, so this write and
            # the cancelled task's own settle cannot both report - whichever wins, the
            # findings survive.
            rec.abort_settle = asyncio.create_task(self._abort_settle(rec, reason),
                                                   name=f"child-abort-{rec.id}")

    async def _abort_settle(self, rec: ChildRecord, reason: str) -> None:
        """Settle an interrupted child AFTER giving its cancelled task a chance to unwind.

        (2026-12 race fix) The settle used to be scheduled with a snapshot taken in the
        SAME synchronous step as the cancel. The child's tool results are captured in
        run_child's finally, which only runs when the cancelled task resumes - so this
        snapshot was usually EMPTY, and an interrupted child that had already read files
        reported "(the child produced no text)" (observed live: steps 1, tool calls 2,
        output 0 chars). Await the unwind first, bounded: if the child settles itself,
        _settle is first-wins and this is a no-op; if it never gets there (cancelled
        before its first step), this still writes the terminal record.
        """
        handles = [h for h in (rec.run_handle, rec.task_handle, rec.continuation_handle)
                   if h is not None and not h.done()]
        if handles:
            try:
                await asyncio.wait(handles, timeout=ABORT_UNWIND_SEC)
            except Exception:
                pass  # a stuck child must never hang the settlement
        await self._settle(rec, partial_report(rec), "aborted", reason)
        # (2026-12) A transport that shields cancellation (the relay WS provider call)
        # cannot be cancelled: the child's loop keeps running until the provider answers,
        # so real findings arrive AFTER the report was written. Fold them into the record
        # and the durable row - the notice already went out, so no second notice. The
        # handles are passed in, because _execute's finally clears rec.run_handle once the
        # wrapper unwinds - reading it here would find nothing to watch.
        self._watch_late_capture(rec, handles)

    def _watch_late_capture(self, rec: ChildRecord,
                            handles: Optional[List[asyncio.Task]] = None) -> None:
        """Fold findings a stopped child still produces into its record (bounded)."""
        live = [h for h in (handles or []) if h is not None and not h.done()]
        if not live or rec.late_capture is not None:
            return

        async def _late() -> None:
            try:
                await asyncio.wait(live, timeout=LATE_CAPTURE_SEC)
            except Exception:
                return
            if rec.status != "aborted" and rec.stop_reason not in ("aborted", "wall-clock"):
                return   # a clean completion has nothing left to fold
            late = partial_report(rec)
            if len(late) <= len(rec.output):
                return
            rec.output = late
            rec.diagnostic = (rec.diagnostic + " | findings arrived after the stop "
                              "(the provider call could not be cancelled mid-flight)")[:4096]
            logger.info(f"[SUBAGENT] late capture for {rec.id}: "
                        f"{len(rec.output)} chars folded into the aborted report")
            if self._on_record is not None:
                try:
                    await self._on_record(rec)
                except Exception as exc:
                    logger.debug(f"[SUBAGENT] late-capture record failed: {exc!r}")

        rec.late_capture = asyncio.create_task(_late(), name=f"child-late-{rec.id}")

    def _descendants(self, child_id: str) -> List[ChildRecord]:
        out: List[ChildRecord] = []
        frontier = [child_id]
        seen: set = set()
        while frontier:
            cid = frontier.pop()
            for rec in self._children.values():
                if rec.parent_conv_id == cid and rec.id not in seen:
                    seen.add(rec.id)
                    out.append(rec)
                    frontier.append(rec.id)
        return out

    async def drain_children(self, parent_conv_id: str, reason: str = "parent stopped",
                             timeout: float = 5.0) -> int:
        """Stop every child of one conversation and AWAIT the unwind (child-first)."""
        ids = self.live_children(parent_conv_id)
        for child_id in ids:
            self.interrupt(child_id, parent_conv_id, reason)
        handles = []
        for child_id in ids:
            rec = self._children.get(child_id)
            if rec is None:
                continue
            # The child's own task, any continuation turn in flight, AND the settle
            # interrupt() scheduled - a record must not still read 'running' when the
            # caller returns.
            # (2026-12) plus the late-capture watcher: without it, a parent Stop returned
            # while a watcher was still folding findings into the record, so the durable
            # row could be written after the caller had already moved on. The caller's
            # timeout still bounds the wait (a shielded provider call may need it).
            for handle in (rec.task_handle, rec.continuation_handle, rec.abort_settle,
                           getattr(rec, "late_capture", None)):
                if handle is not None and not handle.done():
                    handles.append(handle)
        if handles:
            await asyncio.wait(handles, timeout=timeout)
        return len(ids)

    async def close(self) -> None:
        self._closed = True
        for rec in list(self._children.values()):
            # A late-capture watcher must not outlive shutdown (it holds the child's task).
            late = getattr(rec, "late_capture", None)
            if late is not None and not late.done():
                late.cancel()
        for parent in {c.parent_conv_id for c in self._children.values()}:
            await self.drain_children(parent, "runtime shutdown")

    # --- read --------------------------------------------------------------

    def read_output(self, child_id: str, caller: Optional[str] = None,
                    limit_chars: int = 4000) -> Dict:
        rec = self._owned(child_id, caller)
        # (2026-12) A child interrupted mid a shielded provider call settles before its loop
        # appended the tool rows it had already produced, so rec.output can be empty while
        # rec.messages holds real findings - derive from the live list instead of reporting
        # nothing to a caller that explicitly asked for the report.
        output = rec.output or partial_report(rec)
        truncated = len(output) > limit_chars
        return {"child_id": rec.id, "status": rec.status, "stop_reason": rec.stop_reason,
                "output": output[-limit_chars:] if truncated else output,
                "truncated": truncated, "diagnostic": rec.diagnostic,
                "view": rec.view()}


# --- module singleton ---------------------------------------------------

_registry: Optional[ChildRegistry] = None


def get_child_registry() -> Optional[ChildRegistry]:
    return _registry


def set_child_registry(registry: ChildRegistry) -> None:
    global _registry
    _registry = registry
