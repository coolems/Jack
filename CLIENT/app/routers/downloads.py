"""Setup/Downloads progress endpoints (2026-09-21).

Live view of a self-unpacking tool's first-run setup - venv creation, pip installs
and model weight downloads. The data comes from the tiny state file each tool runtime
writes while its setup subprocesses run:

    <runtimes_root>/<tool>/.setup_progress.json

written by tools/tool_bootstrap.py (venv + pip stages) and the tool itself (model
stage, e.g. generate_image). This router only READS that file - it is a pure
information channel for the UI's "Setup / Downloads" section:

    GET /api/setup/status?tool=generate_tool -> {"tool", "updated_at", "stages": {...}}

The runtimes root resolves with the SAME rule as tools/tool_bootstrap.py:
COOLEMS_CLIENT_ROOT/tools/runtimes when running inside the CLIENT process, otherwise
<working_root>/tools (local dev). Any future self-unpacking tool gets this endpoint
for free - no per-tool code needed.

Auth comes from the standard APIMiddleware (this is a normal /api/* route) - nothing
extra to wire here.
"""

import json
import logging
import os

from fastapi import APIRouter, Query

logger = logging.getLogger("COOLEMS")


def _runtimes_root() -> str:
    """Where tool runtimes live on THIS process (same rule as tools/tool_bootstrap.py).

    CLIENT process publishes its own dir as COOLEMS_CLIENT_ROOT at startup; without it
    (pure server / local dev) the legacy <working_root>/tools location is canonical.
    """
    client_root = os.environ.get("COOLEMS_CLIENT_ROOT", "").strip()
    if client_root:
        return os.path.normpath(os.path.join(client_root, "tools", "runtimes"))
    try:
        from app.utils.common import get_working_root
        return os.path.normpath(os.path.join(get_working_root(), "tools"))
    except Exception as e:  # pragma: no cover - working root must exist in practice
        logger.debug(f"setup status: cannot resolve runtimes root ({e})")
        return ""


def _read_setup_progress(tool: str) -> dict | None:
    """Read one tool's .setup_progress.json, or None when it does not exist yet.

    Never raises: a missing/corrupt state file simply means 'no progress to show'.
    """
    root = _runtimes_root()
    if not root:
        return None
    path = os.path.join(root, tool, ".setup_progress.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # corrupt/partial file - treat as absent (writer is atomic anyway)
        logger.debug(f"setup status: unreadable progress for {tool!r}: {e}")
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("stages", {})
    return data


def create_downloads_router() -> APIRouter:
    """Factory (same pattern as the other route modules) - no global state."""
    router = APIRouter(prefix="/api/setup", tags=["setup"])

    @router.get("/status")
    async def get_setup_status(tool: str = Query("generate_tool", min_length=1, max_length=64)):
        """Live setup/download progress for one self-unpacking tool.

        The UI polls this while a tool's first-run setup is in flight (venv -> pip ->
        model) and renders the stages as a "Setup / Downloads" section: per-pip-package
        status, and per-model-file size / downloaded bytes / speed / ETA.

        Returns an empty 'stages' object when no progress file exists yet - the UI
        treats that as 'setup not started (or already cached)'.
        """
        # Defense in depth: the path is joined under runtimes_root/<tool>/... so reject
        # anything that could escape it, even though only a fixed filename is read.
        if "/" in tool or "\\" in tool or ".." in tool or not tool.strip():
            return {"tool": tool, "updated_at": None, "stages": {}}

        data = _read_setup_progress(tool)
        if data is None:
            return {"tool": tool, "updated_at": None, "stages": {}}
        data["tool"] = tool  # echo the requested name (file may predate a rename)
        return data

    @router.get("/tools")
    async def list_setup_tools():
        """All tools that currently have a progress file (for optional UI discovery)."""
        root = _runtimes_root()
        if not root or not os.path.isdir(root):
            return {"tools": []}
        try:
            names = [d for d in os.listdir(root)
                     if os.path.isfile(os.path.join(root, d, ".setup_progress.json"))]
        except Exception as e:  # pragma: no cover - listing failure is not fatal here
            logger.debug(f"setup status: cannot list runtimes root ({e})")
            return {"tools": []}
        return {"tools": sorted(names)}

    return router
