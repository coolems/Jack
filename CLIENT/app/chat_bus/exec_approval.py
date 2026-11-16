"""User approval gate for consent cards.

The AI thinks on a remote SERVER, but python_exec runs code ON THE CLIENT MACHINE, so
the local user has veto power: ToolExecutor emits an 'exec_approval_request' frame with
the exact payload (build_approval_frame()) unless a grant is active, and the turn AWAITs
the decision through deliver_decision -> ExecApprovalState.resolve. Two transports
feed that one function: the chat's 'exec_approval_decision' frame and
POST /api/exec-approval/{conv}/{req}. "Allow until task done" sets THAT kind's flag so
later requests of the same kind skip the question; the state is fresh each turn.

NO timeout - the await blocks until resolve() or the turn stops; fail-closed only where
asking is impossible. Futures live on the SAME uvicorn loop, so no cross-thread
plumbing; cancel_all() withdraws cards a turn leaves unanswered.
"""

import asyncio
import logging
import uuid

logger = logging.getLogger("COOLEMS.ChatBus.ExecApproval")


def build_approval_frame(conv_id: str, request_id: str, code: str,
                         title: str | None = None, description: str | None = None,
                         kind: str | None = None) -> dict:
    """UI frame that renders the interactive approval card in the chat.

    *code* is the payload to approve (source code, or a URL when title/description are
    given); *title*/*description* fill the header and unknown keys are ignored. *kind*
    picks the card variant, e.g. 'network'."""
    frame = {
        "type": "exec_approval_request",
        "conv_id": conv_id,
        "request_id": request_id,
        "code": code,
    }
    if title:
        frame["title"] = title
    if description:
        frame["description"] = description
    if kind:
        frame["kind"] = kind
    return frame


#: Consent card categories - each has its OWN "allow until task done" grant flag.
#: 'desktop' (2026-11-07) is the Computer Use gate: the payload is an APP identity, so
#: its grant means "this AI may drive that application during this run".
#: 'question' is ask_user_question: its grant means "you may decide questions of
#: this kind yourself for the rest of this task" - the AI then states each choice it makes.
ALLOW_TILL_TASK_DONE_KINDS = ("python", "url", "file", "network", "subprocess", "desktop", "question")


def _flag_name(kind: str) -> str:
    """Attribute name of a kind's per-run grant flag (e.g. 'allow_python_till_task_done')."""
    return f"allow_{kind}_till_task_done"


#: Every decision a card may deliver (the same set the HTTP endpoint accepts).
DECISIONS = ("allow", "deny", "allow_category_until_done", "explain")


def deliver_decision(bus, conv_id: str, request_id: str, decision: str,
                     source: str = "http") -> tuple[bool, str]:
    """Deliver one card decision to that chat's live ExecApprovalState.

    ONE resolve path for BOTH transports (WebSocket frame and POST), so they cannot drift
    apart, and every attempt leaves one log line. Returns (resolved, message)."""
    d = str(decision or "").strip().lower()
    if d not in DECISIONS:
        return False, ("decision must be one of: " + ", ".join(DECISIONS))
    channel = getattr(bus, "channels", {}).get(conv_id) if bus is not None else None
    state = getattr(channel, "exec_approval", None) if channel is not None else None
    if state is None or not hasattr(state, "resolve"):
        logger.info(f"[EXEC-APPROVAL] {source} conv={conv_id} request={request_id} "
                    f"decision={d} -> no live approval state, nothing to resolve")
        return False, "No pending consent request for this workspace."
    resolved, _granted = state.resolve(request_id, d)  # per-kind grant flag set inside resolve()
    if not resolved:
        logger.warning(f"[EXEC-APPROVAL] {source} conv={conv_id} request={request_id} "
                       f"decision={d} -> already answered or expired")
        return False, "That approval was already answered or expired."
    logger.info(f"[EXEC-APPROVAL] {source} conv={conv_id} request={request_id} -> {d}")
    return True, ""


def handle_decision_frame(bus, conv_id: str, payload: dict) -> dict:
    """Route one inbound 'exec_approval_decision' frame and build its ack frame.

    conv_id comes from the receiving socket, never from the payload, so a card can only be
    answered on the chat that owns it."""
    rid = str((payload or {}).get("request_id") or "")
    dec = str((payload or {}).get("decision") or "")
    ok, message = deliver_decision(bus, conv_id, rid, dec, source="ws")
    return {"type": "exec_approval_ack", "request_id": rid, "decision": dec,
            "ok": ok, "message": message}


class ExecApprovalState:
    """Per-agentic-run approval state (one instance per turn, fresh each loop run).

    allow_<kind>_till_task_done: ONE flag PER card kind, set by "Allow until task done" on
    a card of that kind - later requests of the same kind skip the question, other kinds
    still ask. All reset by a new state for the next turn.
    """

    def __init__(self):
        # 2026-07-18 rev4: per-category grants - each dialog's "Allow until task done"
        # button only unlocks its own kind, fully detached from the others.
        self.allow_python_till_task_done = False
        self.allow_url_till_task_done = False
        self.allow_file_till_task_done = False
        self.allow_network_till_task_done = False
        self.allow_subprocess_till_task_done = False  # (2026-10-24)
        self.allow_desktop_till_task_done = False    # (2026-11-07) Computer Use cards
        # ask_user_question: "you may decide questions of this kind yourself for
        # the rest of this task". Declined/answered questions are unaffected.
        self.allow_question_till_task_done = False
        # Per-kind "grant already announced" flags: set True the moment a kind's grant fires,
        # so callers can send their one-time system message exactly ONCE per agentic run + kind.
        self._notified: dict[str, bool] = {k: False for k in ALLOW_TILL_TASK_DONE_KINDS}
        self._pending: dict[str, asyncio.Future] = {}
        # request_id -> card kind (set by wait_for_decision) so resolve() knows which flag to set.
        self._request_kind: dict[str, str] = {}

    def is_allowed_till_task_done(self, kind: str) -> bool:
        """True when this run already holds a per-kind grant for *kind* (fail-closed default)."""
        k = str(kind or "").strip().lower()
        if k not in ALLOW_TILL_TASK_DONE_KINDS:
            return False
        return bool(getattr(self, _flag_name(k), False))

    def was_notified(self, kind: str) -> bool:
        """True when a system message for *kind*'s grant was already sent this run."""
        k = str(kind or "").strip().lower()
        return bool(self._notified.get(k, False))

        # --- request side (called from the generation task, on the owner loop) --------

    def new_request_id(self) -> str:
        return uuid.uuid4().hex[:12]

    async def wait_for_decision(self, request_id: str, kind: str | None = None) -> str:
        """Await the user's decision for *request_id*. NO timeout - until answered or stopped.

        *kind* is stored so resolve() can set THAT kind's grant. Returns 'allow' | 'deny' |
        'allow_category_until_done'; cancellation propagates as CancelledError and the pending
        entry is dropped either way."""
        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        k = str(kind or "").strip().lower()
        if k in ALLOW_TILL_TASK_DONE_KINDS:
            self._request_kind[request_id] = k
        logger.info(f"[EXEC-APPROVAL] awaiting user decision request={request_id} "
                    f"(no timeout - waits until the user decides)")
        try:
            return await fut
        finally:
            self._pending.pop(request_id, None)
            self._request_kind.pop(request_id, None)

    # --- resolve side (called from the HTTP endpoint, same uvicorn loop) -----------

    def resolve(self, request_id: str, decision: str) -> tuple[bool, bool]:
        """Fulfil a pending request with *decision*.

        Returns (resolved, newly_granted): False when the id is unknown or already answered
        (the endpoint maps that to HTTP 409); newly_granted only on the click that first
        activates THAT kind's grant, which callers use for their one-time system message."""
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return False, False
        newly_granted = False
        if decision == "allow_category_until_done":
            # 2026-07-18 rev4: per-kind grant - active for the REST OF THIS RUN ONLY and
            # only for the card kind that was clicked (other kinds still ask).
            kind = self._request_kind.get(request_id, "python")
            flag = _flag_name(kind)
            newly_granted = not getattr(self, flag, False)
            setattr(self, flag, True)
            self._notified[kind] = True
        try:
            fut.set_result(decision)
        except asyncio.InvalidStateError:
            # Cancelled in the meantime (turn stopped) — nothing to do.
            return False, False
        logger.info(f"[EXEC-APPROVAL] request={request_id} resolved -> {decision}"
                    + (" (auto-run active until this task ends)" if decision == "allow_category_until_done" else ""))
        return True, newly_granted

    def pending_requests(self) -> list:
        """Live pending request ids as [(request_id, kind)] — what the UI still shows."""
        return [(rid, self._request_kind.get(rid, "python")) for rid in list(self._pending)]

    def cancel_all(self) -> list:
        """Drop every pending request (turn ended or stopped) and report what was dropped.

        Returns [(request_id, kind)] so the caller emits one 'consent_cancelled' frame per card -
        a card with no future can never be answered. Fail-closed: 'deny'.
        """
        dropped = self.pending_requests()
        for rid, fut in list(self._pending.items()):
            if not fut.done() and not fut.cancelled():
                try:
                    fut.set_result("deny")  # fail-closed for any late waiter
                except Exception:
                    pass
            self._pending.pop(rid, None)
        self._request_kind.clear()
        if dropped:
            logger.warning(f"[EXEC-APPROVAL] turn ended with {len(dropped)} unanswered "
                           f"card(s): {[r for r, _k in dropped]}")
        return dropped

