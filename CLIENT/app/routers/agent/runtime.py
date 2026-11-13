"""(2026-12 runtime) subagent + background-job endpoints.

Two routes, both scoped to ONE conversation:

  POST /api/question/{conv_id}/{request_id}   the user's answer to an ask_user_question card
  GET  /api/runtime/{conv_id}                 live children + live jobs + the durable ledger

The question route mirrors /api/exec-approval: it resolves the pending asyncio Future
owned by this chat's ExecApprovalState on the SAME uvicorn loop, so a blocking question
returns inside the tool call. When nobody is waiting (a timed question already returned
PENDING, or the turn ended), the answer is delivered as a runtime notice instead - live
turn -> injected at the loop's next safe boundary, idle chat -> a budgeted follow-up turn.

The GET route is what a reload shows: the live registries are process-local, the ledger
(app.runtime.runtime_store) is durable, and a row whose settlement never arrived stays
'incomplete' - we never invent a finish time.
"""

from typing import Dict

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import logging

logger = logging.getLogger("COOLEMS")


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:
    """Attach the runtime endpoint group to *router*."""

    @router.post("/api/question/{conv_id}/{request_id}")
    async def question_answer(conv_id: str, request_id: str, data: Dict):
        """Answer one ask_user_question card.

        Body: {"answer": "..."} - a chosen option or free text. 'skip' / 'deny' means the
        user declined to decide; the AI is told it must state its own assumption.

        409 when the card is gone (already answered, or the turn ended before it was
        asked again). An answer is never dropped silently: if no tool call is waiting,
        it still reaches the model as a notice / follow-up turn.
        """
        from app.chat_bus import get_chat_bus

        answer = str((data or {}).get("answer", "")).strip()
        if not answer:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "answer must not be empty"}
            )

        bus = get_chat_bus()
        if bus is None:
            return JSONResponse(status_code=409,
                                content={"status": "error", "message": "No chat bus in this process."})
        channel = bus.channels.get(conv_id)
        if channel is None:
            return JSONResponse(status_code=409,
                                content={"status": "error",
                                         "message": "No live workspace for this conversation."})

        state = getattr(channel, "exec_approval", None)
        resolved = False
        if state is not None and hasattr(state, "resolve"):
            # A blocking question is awaiting this Future: the tool call returns the
            # answer itself, so no notice is needed (and sending one would duplicate it).
            resolved, _ = state.resolve(request_id, answer)

        if not resolved:
            # Nobody waiting - timed mode already returned PENDING, or the turn ended.
            delivered = await bus.answer_question(conv_id, request_id, answer)
            if not delivered and not channel.has_pending_question(request_id):
                return JSONResponse(status_code=409,
                                    content={"status": "error",
                                             "message": "That question was already answered."})
            logger.info(f"[QUESTION] conv={conv_id} request={request_id} -> notice")
            return {"status": "ok", "mode": "notice", "delivered": delivered}

        logger.info(f"[QUESTION] conv={conv_id} request={request_id} -> answered in-turn")
        return {"status": "ok", "mode": "answered"}

    @router.get("/api/runtime/{conv_id}")
    async def runtime_state(conv_id: str):
        """Live children + live jobs + durable ledger rows for one workspace.

        Used by the UI on reload (and by the runtime panel): live entries come from the
        registries, history from runtime_state. 'incomplete' rows are reported as such.
        """
        from app.chat_bus import get_chat_bus
        from app.runtime.runtime_store import load_runtime_rows

        children: list = []
        jobs: list = []
        bus = get_chat_bus()
        if bus is not None:
            try:
                from app.runtime.children import get_child_registry
                reg = get_child_registry()
                if reg is not None:
                    children = reg.list_children(conv_id, "descendants")
            except Exception as e:
                logger.debug(f"[RUNTIME] child list failed: {e}")
            try:
                from app.runtime.jobs import get_job_registry
                reg = get_job_registry()
                if reg is not None:
                    jobs = reg.list(conv_id)
            except Exception as e:
                logger.debug(f"[RUNTIME] job list failed: {e}")

        pending = []
        channel = bus.channels.get(conv_id) if bus is not None else None
        if channel is not None:
            try:
                # Flattened for the UI: the channel stores {id, payload, asked_at}, the
                # card renderer wants {request_id, question, options, context}.
                for item in channel.pending_questions():
                    payload = item.get("payload") or {}
                    pending.append({
                        "request_id": item.get("id") or "",
                        "question": payload.get("question") or "",
                        "options": payload.get("options") or [],
                        "context": payload.get("context") or "",
                        # (2026-11-13) what the user asked for, so a restored card still
                        # shows the requirement its offers must satisfy.
                        "requirement": payload.get("requirement") or "",
                        "mode": payload.get("mode") or "blocking",
                        "asked_at": item.get("asked_at"),
                    })
            except Exception:
                pending = []

        try:
            ledger = load_runtime_rows(conv_id)
        except Exception as e:
            logger.debug(f"[RUNTIME] ledger read failed: {e}")
            ledger = []

        # (2026-12 MCP) server state is process-wide, not per conversation: a reload shows
        # which external servers are connected and why any of them failed.
        mcp: list = []
        try:
            from app.mcp.registry import get_registry
            reg = get_registry()
            if reg is not None:
                mcp = reg.status()
        except Exception as e:
            logger.debug(f"[RUNTIME] mcp status failed: {e}")

        return {
            "conv_id": conv_id,
            "children": children,
            "jobs": jobs,
            "pending_questions": pending,
            "ledger": ledger,
            "mcp_servers": mcp,
        }
