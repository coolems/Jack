"""Security review endpoint - what the client end tried that it was not allowed to do.

Read-only view over app.security_events: the in-memory ring of recent events plus
per-kind counters. The authoritative record is <CLIENT>/logs/security_events.jsonl, which
survives restarts; this endpoint exists so an admin can look without opening the file.
"""

import logging
from fastapi import APIRouter, Query

logger = logging.getLogger("COOLEMS")


def create_security_router() -> APIRouter:
    """Factory returning the security review router."""
    router = APIRouter(tags=["security"])

    @router.get("/api/security/events")
    async def list_security_events(limit: int = Query(50, ge=1, le=500)):
        """Recent refused/degraded calls, newest first, with totals since process start.

        Every entry is already sanitised by app.security_events (single line, capped), so
        nothing here can be used to forge a log line or inject markup.
        """
        from app import security_events

        return {
            "events": security_events.recent(limit),
            "summary": security_events.summary(),
            "log_file": security_events.event_file(),
        }

    return router
