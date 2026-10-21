"""Unified consent dialog (2026-07-18) - ONE shared code path for ALL four consent cards.

The four categories are the SAME dialog with different inputs:

    kind      what it gates                                    payload in the frame
    -------   ---------------------------------------------    -------------------------
    python    AI wants to run Python code on THIS machine      the exact source code
    url       browser tool (goto/check_url/download_file) to a local target  the URL
    file      sandboxed python_exec touching a path OUTSIDE working_root     op + path line
    network   sandboxed python_exec connecting OUTSIDE this machine          socket call line

Everything funnels through ONE frame builder (:func:`build_frame`) and ONE ask flow
(:class:`ConsentDialog.ask`): build frame -> send -> await the per-run future owned by
the chat's ExecApprovalState (created fresh each turn in run_generation_turn) -> return.
The UI renders every card from a single kind->config table (CLIENT/static/ui/
approval-dialog.js), so header icon/title, button labels and the auto-approve timer are
per-kind data, not per-kind code.

Decisions: 'allow' | 'deny' | 'allow_category_until_done' ("Allow until task done" - on EVERY card; 2026-07-18 rev4:
    the grant is PER KIND and covers only later consent cards of that same kind) plus the (2026-07-18) 'explain': the user clicked
    "Explain request". The card is answered and nothing is allowed; the caller returns :func:`explain_instruction` to the AI so it
creates/updates a continuation plan, answers in the required two-part format and re-issues
the SAME tool call - which renders a fresh card.

Per-kind auto-approve timers live in settings.json as fully detached key pairs
(<kind>_consent_auto_approve[_seconds]; python keeps its historical python_exec_* keys) -
see config.get_runtime_settings()['approvals']. The FRONTEND starts the countdown; this
module only documents the contract. Fail-closed everywhere: no state / undeliverable
frame => 'unavailable', which callers map to "not allowed".

Kept in its own file + class on purpose (2026-07-18): debug/extend the dialog HERE, not
inside tool_executor.py or python_exec.py.
"""

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("COOLEMS.ChatBus.ConsentDialog")


#: Canonical card kinds - every frame built by build_frame() carries one of these.
KINDS = ("python", "url", "file", "network")

#: Every kind's card offers the "Allow until task done" (allow_category_until_done) button. 2026-07-18 rev4:
#: grants are PER KIND - clicking it on a python card only unlocks later python cards; url /
#: file / network each keep their own independent flag on ExecApprovalState.
ALLOW_TILL_TASK_DONE_KINDS = frozenset(KINDS)


def resolve_kind(kind=None, title=None) -> str:
    """Normalize a frame's kind marker to one of KINDS (legacy-safe).

    Old frames carry no 'kind': python_exec ones have no title, URL-consent ones were
    marked by their title alone - so (no kind + title) maps to 'url', everything else
    defaults to 'python'. New frames always pass an explicit kind.
    """
    k = str(kind or "").strip().lower()
    if k in KINDS:
        return k
    if not k and isinstance(title, str) and title.strip():
        return "url"  # legacy URL-consent frame (identified by its title)
    return "python"


def build_frame(conv_id: str, request_id: str, payload: str, kind: str = "python",
                description: str | None = None) -> dict:
    """The SINGLE canonical 'exec_approval_request' frame builder for all consent cards.

    *payload* is whatever the user must approve (code / URL / op line). The UI renders
    header icon + short title from its own kind->config table; *description* is an extra
    context line shown under the header when provided. Old frames without 'kind' keep
    rendering via resolve_kind()'s legacy fallback, so this is backward compatible.
    """
    frame = {
        "type": "exec_approval_request",
        "conv_id": conv_id,
        "request_id": request_id,
        "code": payload,
        "kind": resolve_kind(kind),
    }
    if description:
        frame["description"] = str(description)
    return frame


def get_approval_state(bus, conv_id):
    """Return this chat's live ExecApprovalState (the per-run future store), or None.

    Callers MUST treat None as fail-closed ("cannot ask" -> not allowed).
    """
    if bus is None or not conv_id:
        return None
    channel = getattr(bus, "channels", {}).get(conv_id)
    if channel is None:
        return None
    state = getattr(channel, "exec_approval", None)
    return state if state is not None and hasattr(state, "wait_for_decision") else None


def explain_instruction(kind: str, title: str) -> str:
    """Agent-facing instruction returned when the user clicks 'Explain request'.

    Nothing was allowed/denied - the AI must create/update a continuation plan for what
    is done so far (planning protocol), then answer in the exact two-part format and
    re-issue the SAME tool call so a fresh approval card appears.
    """
    return (
        "USER ASKED FOR AN EXPLANATION of this pending consent request ("
        f"{title}; kind: {kind}). Nothing was allowed or denied - the action did NOT happen.\n"
        "Do exactly this, in order:\n"
        "1. Create/update a continuation plan file (plan_<UTC timestamp>.md) covering what has been done so far and what remains (planning protocol).\n"
        "2. Reply to the user in EXACTLY two parts:\n"
        "   - UPPER part: one short paragraph - what this request is and why allowing it matters for finishing the task.\n"
        "   - LOWER line, exactly formatted: `for continue plan <plan_name> — just say continue`\n"
        "3. After that reply, re-issue the SAME tool call with the same arguments so the approval card appears again."
    )


@dataclass
class ConsentResult:
    """Outcome of one consent ask (see ConsentDialog.ask)."""
    decision: str                 # 'allow' | 'deny' | 'allow_category_until_done' | 'explain' | 'unavailable'
    explain_instruction: str = ""  # non-empty ONLY when decision == 'explain'

    @property
    def allowed(self) -> bool:
        return self.decision in ("allow", "allow_category_until_done")


class ConsentDialog:
    """One shared ask flow for every consent card kind (2026-07-18).

    Usage::

        from app.chat_bus import get_chat_bus, ConsentDialog

        bus = get_chat_bus()
        result = await ConsentDialog("python").ask(
            websocket=ws, conv_id=conv_id, payload=code, session_timer=session_timer)
        if result.decision == "explain":
            return explain_instruction(...)   # nothing executed - AI explains + re-asks
        if not result.allowed:
            ...fail closed...

    *session_timer* is optional (the sandbox relay has none); it is paused while the user
    decides so approval waits never count as working time. NO timeout on the question
    itself - manual mode waits until the user clicks; auto-approve countdowns are started
    by the FRONTEND per kind from settings.json and simply submit 'allow' for the user.
    """

    def __init__(self, kind: str):
        self.kind = resolve_kind(kind)

    async def ask(self, websocket, conv_id: str, payload: str,
                  description: str | None = None, session_timer=None) -> ConsentResult:
        """Send the card and await the user's decision. Never raises; fail-closed."""
        from app.chat_bus import get_chat_bus

        state = get_approval_state(get_chat_bus(), conv_id)
        if state is None:
            logger.warning("[CONSENT:%s] no approval state for conv=%s - failing closed", self.kind, conv_id)
            return ConsentResult("unavailable")

        # 2026-07-18 rev4: per-kind "Allow until task done" grant from an earlier card of THIS
        # run and the SAME kind only (each dialog category has its own flag).
        if self.kind in ALLOW_TILL_TASK_DONE_KINDS and state.is_allowed_till_task_done(self.kind):
            logger.info("[CONSENT:%s] allow-until-task-done active for this kind - allowed without asking", self.kind)
            return ConsentResult("allow_category_until_done")

        request_id = state.new_request_id()
        try:
            await websocket.send_text(json.dumps(build_frame(
                conv_id, request_id, payload, kind=self.kind, description=description)))
        except Exception as e:
            logger.warning("[CONSENT:%s] frame not delivered (%s) - failing closed", self.kind, e)
            return ConsentResult("unavailable")

        if session_timer is not None:
            session_timer.pause()  # user-decision menu open: no working time while waiting
        try:
            decision = await state.wait_for_decision(request_id, kind=self.kind)
        finally:
            if session_timer is not None:
                session_timer.resume()

        if decision == "explain":
            logger.info("[CONSENT:%s] request=%s -> explain (AI will answer, then re-ask)", self.kind, request_id)
            return ConsentResult("explain", explain_instruction(self.kind, payload[:200]))
        logger.info("[CONSENT:%s] request=%s -> %s", self.kind, request_id, decision)
        return ConsentResult(decision)
