"""User approval gate for python_exec (2026-09-24).

The AI thinks on a remote SERVER, but ``python_exec`` runs code ON THE CLIENT MACHINE.
This module gives the local user veto power over every such execution:

  * ToolExecutor intercepts each python_exec call and — unless auto-run is active for
    this agentic run — emits an ``exec_approval_request`` frame to the UI carrying the
    exact code that would be executed (see build_approval_frame()).
  * The generation task then AWAITs a user decision delivered by the UI through
    deliver_decision() -> ExecApprovalState.resolve(). Two transports feed that one
    function: the chat's own WebSocket frame 'exec_approval_decision' (primary, 2026-11-15)
    and POST /api/exec-approval/{conv_id}/{request_id} (fallback).
  * "Allow until task done" sets that card kind's flag (e.g. ``allow_python_till_task_done``,
    one per category: python / url / file / network) on the run's state: every subsequent
    consent request of THAT KIND in THIS agentic run is allowed without asking. The state is created fresh
    at the start of each turn (run_generation_turn), so a NEW loop asks again — exactly
    the requested reset semantics ("the parameter resets and in a new loop run we will
    be asked again").

There is NO timeout on the question itself: the user gets all the time in the world he
needs to decide. The await simply blocks until resolve() is called (or the turn is
stopped, which cancels the waiter — see below). Fail-closed only applies where we cannot
ask at all: a missing channel/state or an undeliverable frame means "cannot ask" -> deny
with an explanatory message.

Pending decisions live in ONE ExecApprovalState per agentic run, stored on the
ChatChannel as ``channel.exec_approval``. Futures are created on the running event loop
and resolved on the SAME uvicorn loop by whichever transport delivers the answer, so no
cross-thread plumbing is needed. Cancellation (user Stop / superseded turn) propagates
through wait_for_decision as a normal CancelledError and its finally-block drops the
pending entry — nothing leaks. When a turn ends with a card still unanswered,
run_generation_turn calls cancel_all() and emits 'consent_cancelled' so the UI withdraws
the dead card instead of leaving it on screen.
"""

import asyncio
import logging
import uuid

logger = logging.getLogger("COOLEMS.ChatBus.ExecApproval")


def build_approval_frame(conv_id: str, request_id: str, code: str,
                         title: str | None = None, description: str | None = None,
                         kind: str | None = None) -> dict:
    """UI frame that renders the interactive approval card in the chat.

    *code* is whatever payload the user must approve -- python_exec source by default,
    or (2026-07-15 URL consent) a local URL string when title/description are provided.
    The UI uses *title*/*description* for the card header when present and falls back to
    the original "Run this Python code on your machine?" wording otherwise, so older
    clients keep working with new frames (unknown keys are simply ignored).

    *kind* (2026-11-24): optional card variant marker. 'network' renders the OUT-OF-
    SANDBOX NETWORK CONSENT card (warning header + only Allow/Deny buttons, never
    auto-approved); older clients ignore it and render a normal titled card.
    """
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
ALLOW_TILL_TASK_DONE_KINDS = ("python", "url", "file", "network", "subprocess", "desktop")  # (2026-10-24) subprocess; (2026-11-07) desktop


def _flag_name(kind: str) -> str:
    """Attribute name of a kind's per-run grant flag (e.g. 'allow_python_till_task_done')."""
    return f"allow_{kind}_till_task_done"


#: Every decision a card may deliver (the same set the HTTP endpoint accepts).
DECISIONS = ("allow", "deny", "allow_category_until_done", "explain")


def deliver_decision(bus, conv_id: str, request_id: str, decision: str,
                     source: str = "http") -> tuple[bool, str]:
    """Deliver one card decision to that chat's live ExecApprovalState.

    ONE resolve path for BOTH transports — the chat's own WebSocket frame
    'exec_approval_decision' (primary) and POST /api/exec-approval/... (fallback) — so
    they can never drift apart, and every attempt leaves exactly one log line. The
    2026-11-15 "Failed to fetch" bug was undiagnosable because the delivery attempt left
    no trace at all.

    Returns (resolved, message): resolved=False with an explanatory message when the chat
    has no live approval state, or the request id is already answered/expired.
    """
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

    Used by the UI WebSocket receive loop (app/websocket/handler.py). conv_id comes from
    the socket that received the frame, never from the payload, so a card can only ever be
    answered on the chat that owns it.
    """
    rid = str((payload or {}).get("request_id") or "")
    dec = str((payload or {}).get("decision") or "")
    ok, message = deliver_decision(bus, conv_id, rid, dec, source="ws")
    return {"type": "exec_approval_ack", "request_id": rid, "decision": dec,
            "ok": ok, "message": message}


class ExecApprovalState:
    """Per-agentic-run approval state (one instance per turn, fresh each loop run).

    Attributes:
        allow_python_till_task_done / allow_url_till_task_done /
        allow_file_till_task_done / allow_network_till_task_done:
            ONE independent flag PER card category. True after the user picked "Allow until
            task done" on a card of THAT KIND — every later consent request of that same kind
            in this run skips the question; other kinds are unaffected and still ask.
            All reset by creating a new ExecApprovalState for the next turn (run_generation_turn).
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
        """Await the user's decision for *request_id*. NO timeout — waits until the
        user answers (or the turn is stopped).

        *kind* is the card category ('python'/'url'/'file'/'network') - stored so resolve()
        can set THAT kind's grant flag when the user clicks "Allow until task done".

        Returns 'allow' | 'deny' | 'allow_category_until_done' (allow_category_until_done = "allow until task done"). A task cancellation (user Stop or
        superseded turn) propagates as CancelledError — the pending entry is dropped in
        the finally-block either way.
        """
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
        """Fulfil a pending request with *decision* ('allow' | 'deny' | 'allow_category_until_done').

        Returns (resolved, newly_granted): resolved=True when a live pending request was
        answered (False => unknown/already-answered id, endpoint maps that to HTTP 409);
        newly_granted=True ONLY on the "Allow until task done" click that first activates the
        THIS KIND's grant for this run - callers use it to send their one-time system message.
        """
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
        """Drop every pending request (turn ended/stopped) and report what was dropped.

        Returns the dropped cards as [(request_id, kind)] so the caller can emit one
        'consent_cancelled' frame per card: a card whose future is gone can never be
        answered, and leaving it on screen invites a click that does nothing.
        Fail-closed: each waiter gets 'deny'.
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

