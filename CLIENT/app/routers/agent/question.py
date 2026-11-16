"""Question endpoints, both scoped to ONE conversation:

  POST /api/question/{conv_id}/{request_id}   the user's answer to a question card
  GET  /api/runtime/{conv_id}                 cards still unanswered on this chat

The POST resolves the pending Future owned by this chat's ExecApprovalState on the SAME
uvicorn loop, so a blocking question returns inside the tool call; when nobody is
waiting the card is dropped and the caller is told, so a question is never answered
twice. The GET reads the channel's pending store, cleared the moment an answer arrives."""

from typing import Dict

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import logging

logger = logging.getLogger("COOLEMS")


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:
    """Attach the question endpoint group to *router*."""

    @router.post("/api/question/{conv_id}/{request_id}")
    async def question_answer(conv_id: str, request_id: str, data: Dict):
        """Answer one ask_user_question card.

        Body: {"answer": "..."} - a chosen option or free text. 409 when no card is pending.
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
            # answer itself, so nothing else has to deliver it.
            resolved, _ = state.resolve(request_id, answer)

        if not resolved:
            # Nobody waiting - the timed wait already closed this card, or the turn ended.
            # The card is settled either way, so it cannot be answered twice.
            channel.drop_pending_question(request_id)
            return JSONResponse(status_code=409,
                                content={"status": "error",
                                         "message": "That question was already answered."})

        logger.info(f"[QUESTION] conv={conv_id} request={request_id} -> answered in-turn")
        return {"status": "ok", "mode": "answered"}

    @router.get("/api/runtime/{conv_id}")
    async def runtime_state(conv_id: str):
        """Unanswered ask_user_question cards of one workspace, flattened for the card renderer."""
        from app.chat_bus import get_chat_bus

        pending: list = []
        bus = get_chat_bus()
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
                        # What the user asked for, so a restored card still shows the
                        # requirement its offers must satisfy.
                        "requirement": payload.get("requirement") or "",
                        "mode": payload.get("mode") or "blocking",
                        "asked_at": item.get("asked_at"),
                    })
            except Exception:
                pending = []

        return {"conv_id": conv_id, "pending_questions": pending}
