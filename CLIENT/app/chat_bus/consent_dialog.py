"""Unified consent dialog - ONE shared code path for every consent card kind.

Card kinds:
    kind        what it gates                                          payload
    python      AI runs Python code on THIS machine                    the source code
    url         a browser tool going to a local/private target         the URL
    file        sandboxed python_exec outside working_root             op + path line
    network     sandboxed python_exec connecting outside this machine  socket call line
    subprocess  sandboxed python_exec spawning a shell/tool            the command
    desktop     a Computer Use tool driving an application             app + description
    question    an ask_user_question card (not a permission gate)      the question JSON

One frame builder (build_frame), one ask flow (ConsentDialog.ask); the UI renders every
card from a kind->config table (static/ui/approval-dialog.js), so wording and timers are
per-kind data, not per-kind code.

Decisions: 'allow' | 'deny' | 'allow_category_until_done' (per KIND, this run only) |
'explain' (nothing allowed - the caller returns explain_instruction() and re-asks).
Auto-approve timers are settings.json pairs <kind>_consent_auto_approve[_seconds],
counted down by the FRONTEND. Fail-closed: no state or an undeliverable frame =>
'unavailable'.
"""

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("COOLEMS.ChatBus.ConsentDialog")


#: Canonical card kinds - every frame built by build_frame() carries one of these.
#: 'desktop' (2026-11-07) gates Computer Use: the payload is the app identity the AI
#: wants to read/drive, not code, a URL or a path.
#: 'question' is NOT a permission gate: the payload is a question + options the
#: AI asks the human, and the answer is free text. It has no auto-approve setting on
#: purpose - auto-answering a question would invent an answer the user never gave, so the
#: UI never starts a countdown for this kind (see static/ui/approval-dialog.js).
KINDS = ("python", "url", "file", "network", "subprocess", "desktop", "question")

#: Every kind's card offers the "Allow until task done" (allow_category_until_done) button. 2026-07-18 rev4:
#: grants are PER KIND - clicking it on a python card only unlocks later python cards; url /
#: file / network each keep their own independent flag on ExecApprovalState.
ALLOW_TILL_TASK_DONE_KINDS = frozenset(KINDS)


def resolve_kind(kind=None, title=None) -> str:
    """Normalize a frame's kind marker to one of KINDS.

    No 'kind' and no title -> 'python'; a frame marked by its title alone -> 'url'. New
    frames always pass an explicit kind."""
    k = str(kind or "").strip().lower()
    if k in KINDS:
        return k
    if not k and isinstance(title, str) and title.strip():
        return "url"  # legacy URL-consent frame (identified by its title)
    return "python"


def build_frame(conv_id: str, request_id: str, payload: str, kind: str = "python",
                description: str | None = None) -> dict:
    """The SINGLE canonical 'exec_approval_request' frame builder for all consent cards.

    *payload* is what the user must approve (code / URL / op line). The UI takes header
    icon and title from its kind->config table; *description* is an extra context line."""
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

    Nothing was allowed: the AI writes a continuation plan, answers in the required
    two-part format and re-issues the SAME tool call, which renders a fresh card."""
    return (
        "USER ASKED FOR AN EXPLANATION of this pending consent request ("
        f"{title}; kind: {kind}). Nothing was allowed or denied - the action did NOT happen.\n"
        "Do exactly this, in order:\n"
        "1. Create/update a continuation plan file (plan_<UTC timestamp>.md - write_file with path='' puts it in .temp/plans/) covering what has been done so far and what remains (planning protocol).\n"
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
    # (2026-11-07) The card's request id, so callers can record the card as a row of THIS
    # turn's cycle history (app.chat_bus.cycle_recorder). '' when no card was shown
    # (auto-allow grant, or an undeliverable frame).
    request_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision in ("allow", "allow_category_until_done")


class ConsentDialog:
    """One shared ask flow for every consent card kind.

    Usage::

        result = await ConsentDialog("python").ask(
            websocket=ws, conv_id=conv_id, payload=code, session_timer=session_timer)
        if result.decision == "explain":
            return explain_instruction(...)   # nothing executed - AI explains + re-asks
        if not result.allowed:
            ...fail closed...

    *session_timer* is paused while the user decides, so approval waits are not working
    time. NO timeout; auto-approve countdowns belong to the FRONTEND."""

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
            return ConsentResult("explain", explain_instruction(self.kind, payload[:200]), request_id)
        logger.info("[CONSENT:%s] request=%s -> %s", self.kind, request_id, decision)
        return ConsentResult(decision, "", request_id)
