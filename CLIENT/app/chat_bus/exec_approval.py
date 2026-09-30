"""User approval gate for python_exec (2026-09-24).

The AI thinks on a remote SERVER, but ``python_exec`` runs code ON THE CLIENT MACHINE.
This module gives the local user veto power over every such execution:

  * ToolExecutor intercepts each python_exec call and — unless auto-run is active for
    this agentic run — emits an ``exec_approval_request`` frame to the UI carrying the
    exact code that would be executed (see build_approval_frame()).
  * The generation task then AWAITs a user decision delivered by the UI through
    POST /api/exec-approval/{conv_id}/{request_id} -> ExecApprovalState.resolve().
  * "Run until task done" sets ``auto_run_all`` on the run's state: every subsequent
    python_exec of THIS agentic run executes without asking. The state is created fresh
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
and resolved by the HTTP endpoint on the SAME uvicorn loop, so no cross-thread plumbing
is needed. Cancellation (user Stop / superseded turn) propagates through wait_for_decision
as a normal CancelledError and its finally-block drops the pending entry — nothing leaks.
"""

import asyncio
import logging
import uuid

logger = logging.getLogger("COOLEMS.ChatBus.ExecApproval")


def build_approval_frame(conv_id: str, request_id: str, code: str,
                         title: str | None = None, description: str | None = None) -> dict:
    """UI frame that renders the interactive approval card in the chat.

    *code* is whatever payload the user must approve -- python_exec source by default,
    or (2026-07-15 URL consent) a local URL string when title/description are provided.
    The UI uses *title*/*description* for the card header when present and falls back to
    the original "Run this Python code on your machine?" wording otherwise, so older
    clients keep working with new frames (unknown keys are simply ignored).
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
    return frame


class ExecApprovalState:
    """Per-agentic-run approval state (one instance per turn, fresh each loop run).

    Attributes:
        auto_run_all: True after the user picked "Run until task done" — every later
            python_exec of this same run skips the question. Reset by creating a new
            ExecApprovalState for the next turn (run_generation_turn does that).
    """

    def __init__(self):
        self.auto_run_all = False
        self._pending: dict[str, asyncio.Future] = {}

    # --- request side (called from the generation task, on the owner loop) --------

    def new_request_id(self) -> str:
        return uuid.uuid4().hex[:12]

    async def wait_for_decision(self, request_id: str) -> str:
        """Await the user's decision for *request_id*. NO timeout — waits until the
        user answers (or the turn is stopped).

        Returns 'allow' | 'deny' | 'run_all'. A task cancellation (user Stop or
        superseded turn) propagates as CancelledError — the pending entry is dropped in
        the finally-block either way.
        """
        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        logger.info(f"[EXEC-APPROVAL] awaiting user decision request={request_id} "
                    f"(no timeout - waits until the user decides)")
        try:
            return await fut
        finally:
            self._pending.pop(request_id, None)

    # --- resolve side (called from the HTTP endpoint, same uvicorn loop) -----------

    def resolve(self, request_id: str, decision: str) -> bool:
        """Fulfil a pending request with *decision* ('allow' | 'deny' | 'run_all').

        Returns True when a live pending request was resolved, False when the id is
        unknown or already answered (the endpoint maps that to HTTP 409).
        """
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return False
        if decision == "run_all":
            # "Run python_exec till task is done" — active for the REST OF THIS RUN ONLY.
            self.auto_run_all = True
        try:
            fut.set_result(decision)
        except asyncio.InvalidStateError:
            # Cancelled in the meantime (turn stopped) — nothing to do.
            return False
        logger.info(f"[EXEC-APPROVAL] request={request_id} resolved -> {decision}"
                    + (" (auto-run active until this task ends)" if decision == "run_all" else ""))
        return True

    def cancel_all(self) -> None:
        """Drop every pending request (turn ended/stopped). Defensive cleanup only —
        the normal path is cancellation of the awaiting task itself."""
        for rid, fut in list(self._pending.items()):
            if not fut.done() and not fut.cancelled():
                try:
                    fut.set_result("deny")  # fail-closed for any late waiter
                except Exception:
                    pass
            self._pending.pop(rid, None)
