"""Stop-generation and python_exec user-approval endpoints.

STOP: POST /api/stop/{conv_id} is bus-aware - it routes through ChatBus stop() first,
falls back to the legacy stop_events dict, and reports an honest success when nothing is
live to abort (the UI contract: clicking Stop always clears the red state).

EXEC APPROVAL: POST /api/exec-approval/{conv_id}/{request_id} resolves the pending
asyncio.Future owned by this chat's ExecApprovalState. The UI answers over the chat's own
WebSocket instead (app/websocket/handler.py); this endpoint is the fallback and both go
through app.chat_bus.exec_approval.deliver_decision(). Same uvicorn event loop as the
generation task, so a plain set_result is safe.
"""

from typing import Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import logging

logger = logging.getLogger("COOLEMS")


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:
    """Attach the exec-approval and stop endpoints to *router*."""
    @router.post("/api/stop/{conv_id}")
    async def stop_generation(conv_id: str):
        """Stop the current generation for a conversation."""
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

    # ===== CONSENT CARD DECISION ENDPOINT (2026-09-24) ============================
    # FALLBACK transport of a consent-card decision. The PRIMARY path since 2026-11-15 is
    # the 'exec_approval_decision' frame on the chat's own WebSocket (app/websocket/
    # handler.py): a one-shot POST dies when the page is torn down or the workspace is
    # switched, which is how a click could be lost with "Failed to fetch". This endpoint
    # stays for non-UI callers and old clients; both transports funnel through
    # app.chat_bus.exec_approval.deliver_decision() so they cannot drift apart.

    @router.post("/api/exec-approval/{conv_id}/{request_id}")
    async def exec_approval_decision(conv_id: str, request_id: str, data: Dict):
        """Resolve a pending consent card for one chat (fallback transport).

                Body: {"decision": "allow" | "deny" | "allow_category_until_done" | "explain"}
                  - allow   : run this ONE piece of code / open this ONE target
                  - deny    : do NOT run it (the AI is told the user declined)
                  - allow_category_until_done : "run until task done" - every card of THIS agentic
                              run's kind executes without asking; resets on the next loop run

                409 when the request id is unknown or already answered (e.g. the turn was stopped
                in between), 400 on an unknown decision.

        """
        from app.chat_bus import get_chat_bus
        from app.chat_bus.exec_approval import deliver_decision

        resolved, message = deliver_decision(get_chat_bus(), conv_id, request_id,
                                             (data or {}).get("decision", ""), source="http")
        if not resolved:
            status = 400 if message.startswith("decision must be") else 409
            return JSONResponse(status_code=status,
                                content={"status": "error", "message": message})
        return {"status": "ok", "decision": str((data or {}).get("decision", "")).strip().lower()}



    @router.get("/api/my-permissions")
    async def get_my_permissions(request: Request):
        """Report the authenticated role, for display only."""
        role = getattr(request.state, 'api_role', 'unknown')

        return {
            "role": role,
            "allowed_tools": None,   # Enforced on SERVER (tools_response is role-filtered)
            "allowed_models": None,  # Enforced on SERVER (model lists are pre-filtered)
            "has_all_tools": True,   # Client sees no local restriction; server enforces real limits
            "has_all_models": True,
        }

    pass  # fallback; every register() above attaches at least one route
