"""Search engine endpoints: GET/POST /api/search-engines.
<CLIENT>/config/search_engines.json is the single source of truth for web search;
the Settings UI renders/edits it through these endpoints and the web_search tool
reads the same file live on every search, so saves apply immediately (no restart)."""

from typing import Dict

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import logging

logger = logging.getLogger("COOLEMS")


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:
    """Attach this endpoint group to *router* (verbatim move from the original flat file)."""
    # ===== SEARCH ENGINES ENDPOINTS =====
    # <CLIENT>/config/search_engines.json is the single source of truth for web search.
    # The Settings UI renders/edits it through these two endpoints; the web_search tool
    # reads the same file live on every search, so saves apply immediately (no restart).

    @router.get("/api/search-engines")
    async def get_search_engines():
        """Return the full engine list from config/search_engines.json.

        On a missing/corrupt file load_search_engines() returns [] and logs an error -
        the UI then shows that state instead of inventing engines (no fallback lists).
        """
        try:
            from config import load_search_engines

            return {"engines": load_search_engines()}
        except Exception as e:
            logger.error(f"Failed to read search engines: {e}")
            return JSONResponse(status_code=500, content={"detail": f"Failed to read search engines: {str(e)}"})

    @router.post("/api/search-engines")
    async def save_search_engines_endpoint(data: Dict):
        """Replace the engine list in config/search_engines.json.

        Expects {"engines": [ ... ]}. Every entry is validated (id format, name,
        enabled bool, priority int >= 1, settings object); on any error the file is
        left untouched and a 400 with the reason is returned.
        """
        try:
            from config import load_search_engines, save_search_engines

            engines = data.get("engines")
            if not isinstance(engines, list):
                return JSONResponse(status_code=400, content={"detail": "Body must be {'engines': [...]}"})

            try:
                save_search_engines(engines)
            except ValueError as ve:
                return JSONResponse(status_code=400, content={"detail": str(ve)})

            logger.info(f"Search engines saved: {len(load_search_engines())} engine(s)")
            return {"status": "ok", "message": "Search engines saved successfully.", "engines": load_search_engines()}
        except Exception as e:
            logger.error(f"Failed to save search engines: {e}")
            return JSONResponse(status_code=500, content={"detail": f"Failed to save search engines: {str(e)}"})


    pass  # fallback; every register() above attaches at least one route
