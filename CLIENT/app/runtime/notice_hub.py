"""notice_hub — settlements become runtime notices on the owner's chat (client side).

The single place where a settled child or a settled job turns into text the model reads.
It owns three things:

  * the notice TEXT (bounded, so a huge report never lands in the context window — the
    full stream stays reachable through job_output / the spill path),
  * the ROUTING: a live turn gets the notice at its next safe boundary, an idle chat gets
    a follow-up turn (budgeted by the channel's consecutive-wake cap),
  * the UI frames + the durable ledger row, so a reload shows what happened even after the
    process is gone.

Deliberately NOT importing the registries: ChatBus._cascade_stop reads them lazily, and
this module only receives records. That keeps the graph acyclic:
    react_loop -> channel -> notice lane -> hub -> bus.
"""

import logging
from typing import Optional

from config import (
    NOTICE_INLINE_BYTES,
    SUBAGENT_NOTICE_INLINE_BYTES,
    JOB_INLINE_OUTPUT_BYTES,
)
from app.runtime.runtime_store import save_runtime_row

logger = logging.getLogger("COOLEMS.Runtime.NoticeHub")


def _clip(text: str, limit: int) -> str:
    """Keep the TAIL: the end of a report/output is the part the model needs most."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return "...[earlier output trimmed]...\n" + text[-limit:]


def child_notice_text(rec) -> str:
    """The settled-child report, bounded. Header is added by logic.runtime_notice."""
    from app.runtime.children import observed_work, partial_report
    steps, tool_calls = observed_work(rec)
    lines = [f"Child {rec.id} ({rec.mode}, depth {rec.depth}): {rec.label}",
             f"stop_reason: {rec.stop_reason or 'completed'}"]
    # (2026-11-30) `turns` counts run_child CALLS, so a child that did 31 tool calls used
    # to read as "turns: 1" - the parent judged the work as barely done. Report the child's
    # provider STEPS and its tool calls; turns only matters when it was continued.
    # (2026-12) observed_work(), not the record counters: a child cut off inside a long
    # provider/tool call never reaches run_child's finally, so its counters stayed 0 while
    # its own message list held real tool results - it read as "did nothing".
    lines.append(f"steps: {steps}, tool calls: {tool_calls}"
                 + (f", continuations: {rec.continuations}" if rec.continuations else ""))
    if rec.diagnostic:
        lines.append(f"diagnostic: {rec.diagnostic[:400]}")
    # (2026-12) The report is derived AT NOTICE TIME, not only from the settled snapshot. A
    # child interrupted mid a shielded provider call settles before its loop appended the
    # tool rows it had already produced, so the snapshot was empty while the child's own
    # message list held real findings (observed live: steps 4 / tool calls 4, report empty).
    body_text = rec.output or partial_report(rec)
    body = _clip(body_text, SUBAGENT_NOTICE_INLINE_BYTES)
    if body:
        lines.append("--- report ---\n" + body)
    else:
        lines.append("--- report ---\n(the child produced no text)")
    # (2026-12) A stop that lands inside an in-flight provider call settles the record
    # BEFORE the child's loop appended anything (the relay WS call cannot be cancelled).
    # Say so, so the parent does not read an empty report as "the child did nothing" - the
    # late-capture watcher folds the findings into the record, and send_message retrieves
    # the child's full conversation.
    live = [h for h in (getattr(rec, "run_handle", None), getattr(rec, "task_handle", None))
            if h is not None and not h.done()]
    if live and rec.stop_reason == "aborted":
        lines.append("[the child was stopped mid a provider call that cannot be cancelled - "
                     "it may still produce findings after this notice. Its record is updated "
                     "when it unwinds; send_message can retrieve its full conversation.]")
    if len(body_text) > SUBAGENT_NOTICE_INLINE_BYTES:
        lines.append(f"[report trimmed to {SUBAGENT_NOTICE_INLINE_BYTES} chars; "
                     "the child's full conversation is kept - send_message can continue it]")
    return "\n".join(lines)


def job_notice_text(rec) -> str:
    """The settled-job notice: status + unread output that fits inline.

    Carries only what the model has NOT already received. The terminal RESULT is never
    duplicated here: job_output owns it (the registry hands it out exactly once), so the
    notice points at that tool instead of pasting the same payload twice. Unread stream
    bytes are included because the model may never have read them.
    """
    lines = [f"Job {rec.id} ({rec.kind}): {rec.label}",
             f"status: {rec.status}",
             f"cause: {rec.cause or 'producer'}"]
    if rec.detail:
        lines.append(f"detail: {rec.detail[:300]}")
    if rec.progress:
        lines.append(f"progress: {rec.progress}")
    unread = rec.ring.read_text(rec.cursor)[0]
    inline = _clip(unread, JOB_INLINE_OUTPUT_BYTES)
    if inline:
        lines.append("--- output since your last read ---\n" + inline)
    else:
        lines.append("(no unread output - you already read this stream)")
    if rec.result and not rec.result_handed_out:
        lines.append(f"[terminal result not shown here - call job_output({rec.id}) "
                     "to receive it]")
    if len(unread) > JOB_INLINE_OUTPUT_BYTES:
        lines.append(f"[{len(unread) - JOB_INLINE_OUTPUT_BYTES} chars not shown - "
                     "call job_output for the rest]")
    if rec.ring.spill_path:
        lines.append(f"[complete stream file: {rec.ring.spill_path}]")
    lines.append("Do not busy-poll: read this job once, then keep working on independent "
                 "steps. Collect every still-relevant job before your final answer.")
    return "\n".join(lines)


async def _record(bus, conv_id: str, entity_kind: str, entity_id: str, **data) -> None:
    """Record one runtime entity on its owner's channel (2026-12).

    (2026-12 fix) The parameter was named `kind`, which collided with the `kind=` keyword
    the JOB callers pass (CycleRecorder.job takes kind='python'/'bash'). Every job call
    raised "TypeError: _record() got multiple values for argument 'kind'" BEFORE reaching
    the try block, so the error escaped _record and the settlement handler swallowed it at
    debug level - the job's UI row NEVER painted, for the start row or the settle row.
    Renamed to entity_kind; the payload key stays `kind`, which is what the recorder and
    the UI read.

    Goes through ChatChannel.record_runtime(), which routes to the live turn's
    CycleRecorder when there is one (so the entity is a row of that turn's block AND lands
    in the persisted turn_log payload), and to a plain frame when the turn already ended —
    the normal case for a settlement. Never raises: a UI failure must not break a
    settlement, and the notice still carries the information to the model.
    """
    ch = bus.channels.get(conv_id) if conv_id else None
    if ch is None:
        return
    try:
        await ch.record_runtime(entity_kind, entity_id, **data)
    except Exception as exc:
        logger.debug(f"[RUNTIME] {entity_kind} row {entity_id} could not be recorded: {exc!r}")


async def record_child(bus, rec) -> None:
    """Ledger row + UI row for one child turn, WITHOUT a notice.

    Used for CONTINUATION turns: their reply is returned to the caller as a tool result,
    so a notice would deliver the same text twice. The durable row still matters - a
    capped or aborted continuation otherwise leaves no trace of what the child did.
    """
    owner = getattr(rec, "owner_conv_id", "") or rec.parent_conv_id
    # (2026-12) Counters come from observed_work(), exactly like the notice text. The record
    # counters are written only in run_child's finally, which a cancel inside a long
    # provider/tool call skips - so an interrupted child showed its real work in the notice
    # and 0/0 in the durable ledger row and the UI row (the same divergence the notice fix
    # was written to end, still present on the record path).
    from app.runtime.children import observed_work, partial_report
    steps, tool_calls = observed_work(rec)
    body = rec.output or partial_report(rec)
    # (2026-12) The ledger row is the ONLY trace of a child that settled after its turn
    # ended (the turn_log row lives in the turn payload, which is gone for that case), and
    # it had no columns for how much work the child did - so a reload could not answer
    # "what did this workspace delegate, and how much of it finished". Fold the counters
    # into detail alongside the terminal reason.
    detail = f"{rec.stop_reason or 'completed'} steps={steps} tools={tool_calls}"
    if rec.continuations:
        detail += f" continuations={rec.continuations}"
    if rec.diagnostic:
        detail += f" - {rec.diagnostic}"
    save_runtime_row(conv_id=owner, kind="child", status=rec.status,
                     label=rec.label, mode=rec.mode, depth=rec.depth,
                     detail=detail, result=body[:2000],
                     row_id=rec.id, finished=True)
    await _record(bus, owner, "child", rec.id, label=rec.label,
                  mode=rec.mode, depth=rec.depth, status=rec.status,
                  stop_reason=rec.stop_reason, output=_clip(body, 400),
                  turns=rec.turns, steps=steps, tool_calls=tool_calls,
                  at_ts=rec.finished_at)


async def on_child_settled(bus, rec) -> None:
    """One notice per settled child + its ledger row + a UI frame.

    Everything routes to rec.owner_conv_id (the ROOT chat of the lineage), not
    rec.parent_conv_id: a grandchild's parent is a child id, so delivering there would
    create a phantom channel on the bus and the real chat would never hear the report.
    """
    owner = getattr(rec, "owner_conv_id", "") or rec.parent_conv_id
    text = child_notice_text(rec)
    try:
        await bus.deliver_notice(owner, text, "subagent",
                                 meta={"child_id": rec.id, "stop_reason": rec.stop_reason})
    except Exception as exc:
        logger.debug(f"[SUBAGENT] notice delivery failed for {rec.id}: {exc!r}")
    await record_child(bus, rec)


async def record_job(bus, rec) -> None:
    """Ledger row + UI row for one settled job, WITHOUT a notice.

    (2026-12 fix) This used to live inside on_job_settled, which the registry only calls
    when the settlement was NOT claimed. A claimed settlement (job_output(wait=true)
    released on it, a read that handed out the result, or job_kill's own answer) delivered
    nothing to the model - so it also wrote NO ledger row and left the UI's job row stuck
    at 'running' forever. Observed live: a killed job had no row in runtime_state at all.
    The row is not a delivery to the model; it is the record the user sees on reload, so it
    must fire for EVERY settlement, claimed or not.
    """
    save_runtime_row(conv_id=rec.owner or "", kind="job", status=rec.status,
                     label=rec.label, mode=rec.kind, detail=rec.detail,
                     result=rec.result, row_id=rec.id, finished=True)
    await _record(bus, rec.owner or "", "job", rec.id, label=rec.label, kind=rec.kind,
                  status=rec.status, detail=rec.detail[:200], progress=rec.progress,
                  output=_clip(rec.ring.read_text(0)[0], 400),
                  spill_path=rec.ring.spill_path, at_ts=rec.finished_at)


async def on_job_settled(bus, rec) -> None:
    """One notice per settled job. The ledger/UI row is a separate handler (record_job)."""
    text = job_notice_text(rec)
    try:
        await bus.deliver_notice(rec.owner or "", text, "job",
                                 meta={"job_id": rec.id, "status": rec.status})
    except Exception as exc:
        logger.debug(f"[JOBS] notice delivery failed for {rec.id}: {exc!r}")


async def on_job_started(bus, rec) -> None:
    """The job's first row, so the user sees background work the moment it starts.

    (2026-12) The durable row too, not just the UI frame. Without a start row a job that
    died with the process left NO trace at all, so the ledger could never answer "did this
    job finish or did the process die mid-run" - finished_at stays NULL here, which is what
    load_runtime_rows() reports as 'incomplete'.
    """
    save_runtime_row(conv_id=rec.owner or "", kind="job", status=rec.status,
                     label=rec.label, mode=rec.kind, row_id=rec.id)
    await _record(bus, rec.owner or "", "job", rec.id, label=rec.label, kind=rec.kind,
                  status=rec.status, progress=rec.progress)


async def on_job_output(bus, rec, text: str, channel: str) -> None:
    """Live job output as a UI frame only — NEVER the model's cursor.

    The model reads the ring through job_output; painting live output in the turn block
    must not steal bytes it has not seen yet.
    """
    ch = bus.channels.get(rec.owner) if rec.owner else None
    if ch is None:
        return
    try:
        await ch.emit_frame({
            "type": "cycle_job_output", "conv_id": rec.owner,
            "turn_id": getattr(ch, "turn_id", "") or "",
            "job_id": rec.id, "kind": rec.kind, "channel": channel,
            "text": text[:600], "at_ts": None,
        })
    except Exception:
        pass


def wire_runtime(bus, *, spill_dir: str,
                 child_registry: Optional["ChildRegistry"] = None) -> "ChildRegistry":
    """Build + wire the registries to this bus (called once from app startup).

    Returns the child registry so callers can hand it to the tool layer.
    """
    from app.runtime.children import ChildRegistry
    from app.runtime.jobs import create_job_registry

    jobs = create_job_registry(spill_dir)
    jobs.set_settlement_handler(lambda rec: on_job_settled(bus, rec))
    # The ledger/UI row is wired separately from the notice: it fires on EVERY settlement,
    # including a claimed one (job_output/job_kill already delivered the outcome to the
    # model, so the notice stays silent but the row must still exist).
    jobs.set_record_handler(lambda rec: record_job(bus, rec))
    jobs.set_output_observer(lambda rec, text, ch: on_job_output(bus, rec, text, ch))
    jobs.set_start_observer(lambda rec: on_job_started(bus, rec))

    children = child_registry or ChildRegistry()
    children.wire(run_child=_child_executor(bus),
                  on_settle=lambda rec: on_child_settled(bus, rec),
                  # A continuation turn is returned as a tool result, so it gets the
                  # ledger/UI row only - never a second notice for the same reply.
                  on_record=lambda rec: record_child(bus, rec))
    from app.runtime.children import set_child_registry
    set_child_registry(children)
    logger.info("[RUNTIME] subagents + background jobs wired to the ChatBus")
    return children


def _child_executor(bus):
    """Closure that builds a child's context from the bus' own turn inputs.

    The bus holds provider / model / orchestrator / agent / ws_key — exactly what a child
    needs. working_root comes from the conversation row, same as a normal turn.
    """
    async def _run(rec, context, continuation: str = "") -> str:
        from app.runtime.child_run import child_context, run_child
        # Package import, not the submodule path: app.utils.common re-exports every public
        # name, so this keeps working if the module layout moves again.
        from app.utils.common import resolve_working_root_for_conv
        # The ROOT chat owns the channel, the notice lane and the working root. A
        # grandchild's parent_conv_id is a CHILD id, so looking that up on the bus would
        # return None (-> "child context has no parent channel") and resolve the working
        # root of a conversation that does not exist.
        owner = getattr(rec, "owner_conv_id", "") or rec.parent_conv_id
        channel = bus.channels.get(owner)
        try:
            working_root = resolve_working_root_for_conv(owner)
        except Exception:
            working_root = None
        ctx = child_context(
            provider=bus._provider,
            model=bus._model_name,
            api_key=bus._ws_key,
            agent_name=bus._agent.get_name(),
            dna_content=bus._agent.get_identity(),
            generic_tool_prompt=context.get("generic_tool_prompt") or "",
            tool_orchestrator=bus._tool_orchestrator,
            ollama_tools=context.get("ollama_tools") or [],
            channel=channel,
            history=context.get("history"),
            working_root=working_root,
            # (2026-12 thinking) verbatim: a bool or the parent's canonical level.
            enable_thinking=context.get("enable_thinking", False),
            temperature=float(context.get("temperature") or 0.6),
            role=context.get("role"),
        )
        return await run_child(rec, ctx, continuation)

    return _run
