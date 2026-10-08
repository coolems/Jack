"""Stop-generation + python_exec user-approval endpoints.

STOP (STALE-TASK FIX 2026-09-10): POST /api/stop/{conv_id} is BUS-AWARE - it routes
through ChatBus stop() first, falls back to the legacy stop_events dict, and reports
an honest success when there is nothing live to abort (the UI contract: clicking Stop
ALWAYS clears the red state).

EXEC APPROVAL (2026-09-24): POST /api/exec-approval/{conv_id}/{request_id} resolves the
pending asyncio.Future owned by this chat's ExecApprovalState. Same uvicorn event loop
as the generation task, so a plain set_result is safe - no cross-thread plumbing."""

from typing import Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import logging

logger = logging.getLogger("COOLEMS")


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:
    """Attach this endpoint group to *router* (verbatim move from the original flat file)."""
    @router.post("/api/stop/{conv_id}")
    async def stop_generation(conv_id: str):
        """Stop the current generation for a conversation.

        STALE-TASK FIX (2026-09-10, "No active generation found" bug): this endpoint is
        now BUS-AWARE. The red stop button in the UI calls it whenever the chat shows as
        working - and that display state comes from the SAME ChatBus channel status
        (/api/conversations/{id}/status + chat_status frames). So when the button is red,
        a live channel with a running task ALWAYS exists here; we route through bus.stop()
        which sets the channel's stop event (cooperative abort at every loop boundary) and
        also CANCELS the background task (hard backstop for tool-execution windows that do
        not poll the event). The old code read only the legacy stop_events dict, whose
        entry is removed as soon as a turn ends - so after a crash/reopen it 404'd with
        "No active generation found" while the UI still showed the chat as working.

        Fallback (bus missing or no channel): the legacy stop_events dict path stays for
        compatibility, and if even that is empty we return an honest "nothing to stop"
        success - the caller's contract is that clicking Stop ALWAYS clears the red state,
        never errors on a ghost session.
        """
        from app.chat_bus import get_chat_bus

        bus = get_chat_bus()
        if bus is not None:
            stopped = await bus.stop(conv_id)
            if stopped:
                logger.info(f"Stop requested for conversation {conv_id} (via ChatBus)")
                return {"status": "ok", "message": "Generation stopped"}

        # Fallback path: legacy stop_events dict (bus unavailable or channel already gone).
        entry = stop_events.get(conv_id)
        if entry is not None:
            event = entry["event"] if isinstance(entry, dict) else entry
            try:
                event.set()
            except AttributeError as e:
                logger.error(f"Stop failed for conversation {conv_id}: bad stop_events entry type {type(entry).__name__}")
                return JSONResponse(
                    status_code=500,
                    content={"status": "error", "message": f"Internal error setting stop event: {e}"}
                )
            logger.info(f"Stop requested for conversation {conv_id} (via legacy stop_events)")
            return {"status": "ok", "message": "Generation stopped"}

        # No live session at all (ghost state after a crash/reopen, or the turn already
        # finished). Report success: there is nothing to abort and the UI must clear.
        logger.info(f"Stop requested for conversation {conv_id}: no active generation - nothing to stop")
        return {"status": "ok", "message": "No active generation - already stopped"}

    # ===== PYTHON_EXEC USER APPROVAL ENDPOINT (2026-09-24) ======================
    # The chat UI's approval card posts the user's decision here. It resolves the
    # pending asyncio.Future owned by this chat's ExecApprovalState (see
    # app/chat_bus/exec_approval.py). Same uvicorn event loop as the generation
    # task, so a plain set_result is safe - no cross-thread plumbing.

    @router.post("/api/exec-approval/{conv_id}/{request_id}")
    async def exec_approval_decision(conv_id: str, request_id: str, data: Dict):
        """Resolve a pending python_exec approval for one chat.

        Body: {"decision": "allow" | "deny" | "run_all"}
          - allow   : run this ONE piece of code
          - deny    : do NOT run it (the AI is told the user declined)
          - run_all : "run until task done" - every python_exec of THIS agentic
                      run executes without asking; resets on the next loop run

        409 when the request id is unknown or already answered (e.g. the user was
        too slow and it timed out, or the turn was stopped in between).
        """
        from app.chat_bus import get_chat_bus

        decision = str((data or {}).get("decision", "")).strip().lower()
        if decision not in ("allow", "deny", "run_all"):
            return JSONResponse(
                status_code=400,
                content={"status": "error",
                         "message": "decision must be one of: allow, deny, run_all"}
            )

        bus = get_chat_bus()
        channel = bus.channels.get(conv_id) if bus is not None else None
        state = getattr(channel, "exec_approval", None) if channel is not None else None
        if state is None or not hasattr(state, "resolve"):
            logger.info(f"[EXEC-APPROVAL] no live approval state for conv={conv_id} - nothing to resolve")
            return JSONResponse(
                status_code=409,
                content={"status": "error",
                         "message": "No pending python_exec approval for this workspace."}
            )

        if not state.resolve(request_id, decision):
            return JSONResponse(
                status_code=409,
                content={"status": "error",
                         "message": "That approval was already answered or expired."}
            )

        logger.info(f"[EXEC-APPROVAL] conv={conv_id} request={request_id} -> {decision}")
        return {"status": "ok", "decision": decision}



    @router.get("/api/my-permissions")
    async def get_my_permissions(request: Request):
        """Get current user's role.

        NOTE: The CLIENT does not resolve permissions locally — the SERVER
        enforces model/tool restrictions and pre-filters all lists it sends.
        This endpoint reports the authenticated role for display only.
        """
        role = getattr(request.state, 'api_role', 'unknown')

        return {
            "role": role,
            "allowed_tools": None,   # Enforced on SERVER (tools_response is role-filtered)
            "allowed_models": None,  # Enforced on SERVER (model lists are pre-filtered)
            "has_all_tools": True,   # Client sees no local restriction; server enforces real limits
            "has_all_models": True,
        }

    pass  # fallback; every register() above attaches at least one route
