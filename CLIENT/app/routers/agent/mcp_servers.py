"""MCP server management endpoints (2026-12): GET/POST /api/mcp/servers.

The Settings > MCP tab reads and writes CLIENT/config/mcp_servers.json through these two
routes, so enabling/disabling/editing an external MCP server is a normal UI action instead
of a hand-edit of the JSON file. The file stays the single source of truth (persisted for
later use), and every save re-wires the live registry immediately - no restart needed:

  * GET  -> all entries (enabled AND disabled) + their live connection state, so the tab
            shows what is configured vs what actually connected;
  * POST -> full replacement of the "servers" list. Each entry passes the SAME validator
            as startup (_entry in app/mcp/config.py), names must be unique, and a bad list
            is rejected (400) WITHOUT touching the file - the last good config survives.

Security model unchanged: enabling here is still a HUMAN decision made in this UI, and the
first use of any server's tool still asks its consent card (subprocess for stdio, url for
http). The CLIENT never auto-enables anything.
"""

import json
import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger("COOLEMS")

#: <CLIENT> root anchored to this file (app/routers/agent -> 3 up), never CWD-relative.
_CLIENT_ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


def _entry_dict(s) -> Dict[str, Any]:
    """McpServerConfig -> the JSON shape mcp_servers.json stores (stable key order)."""
    return {
        "name": s.name,
        "enabled": bool(s.enabled),
        "transport": s.transport,
        "command": s.command,
        "args": list(s.args),
        "env": dict(s.env),
        "cwd": s.cwd,
        "url": s.url,
        "roles": sorted(s.roles),
        "timeout_sec": s.timeout_sec,
    }


def _rows_with_status(servers: List[Any], reg) -> List[Dict[str, Any]]:
    """Config entries + live connection state from the registry (None-safe)."""
    live = {}
    if reg is not None:
        for row in reg.status():
            live[row["name"]] = row
    rows: List[Dict[str, Any]] = []
    for s in servers:
        row = _entry_dict(s)
        st = live.get(s.name) if reg is not None else None
        row["connected"] = bool(st and st.get("connected"))
        row["error"] = (st or {}).get("error", "")
        row["tools"] = int((st or {}).get("tools", 0))
        rows.append(row)
    return rows


async def _rewire_registry() -> None:
    """Close the current registry (if any) and rebuild it from mcp_servers.json.

    Runs on the live event loop, so a stdio server can be started here; a failure is
    logged, never raised - a settings save must not 500 because an external program
    refused to start (the tab then shows 'not connected' + the error)."""
    from app.mcp.registry import connect_all, get_registry, set_registry, wire_mcp

    old = get_registry()
    if old is not None:
        try:
            await old.close_all()
        except Exception as e:  # pragma: no cover - teardown must never raise
            logger.debug(f"[MCP] close before re-wire failed (non-fatal): {e}")

    spill = os.path.normpath(os.path.join(_CLIENT_ROOT, ".temp", "mcp"))
    try:
        reg = wire_mcp(spill_dir=spill)
    except Exception as e:  # pragma: no cover - MCP must never break a settings save
        logger.warning(f"[MCP] re-wire after settings save failed (non-fatal): {e}")
        set_registry(None)
        return
    if reg is None:
        # Nothing enabled any more -> clear the stale registry explicitly (wire_mcp only
        # installs a NEW one, it never removes an old one).
        set_registry(None)
        return
    try:
        state = await connect_all()
        live = sorted(n for n, ok in state.items() if ok)
        dead = sorted(n for n, ok in state.items() if not ok)
        logger.info(f"[MCP] re-wired after settings save - connected: {live or 'none'}"
                    + (f"; failed: {dead}" if dead else ""))
    except Exception as e:  # pragma: no cover
        logger.warning(f"[MCP] connect after re-wire failed (non-fatal): {e}")


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:

    @router.get("/api/mcp/servers")
    async def get_mcp_servers():
        """All configured MCP servers + live state. For the Settings > MCP tab."""
        from app.mcp.config import SERVERS_FILE, load_servers
        try:
            from app.mcp.registry import get_registry
            reg = get_registry()
        except Exception:  # pragma: no cover - registry import must never break the tab
            reg = None

        servers, problems = load_servers()
        return {"servers": _rows_with_status(servers, reg),
                "problems": problems,
                "config_file": SERVERS_FILE}

    @router.post("/api/mcp/servers")
    async def save_mcp_servers(data: Dict):
        """Replace the whole servers list in mcp_servers.json and re-wire live.

        Body: {"servers": [ {name, enabled, transport, command?, args?, env?, cwd?,
                             url?, roles?, timeout_sec?}, ... ]} - every entry is
        validated with the startup validator; on ANY problem nothing is written."""
        from app.mcp.config import SERVERS_FILE, _entry, load_servers

        raw_servers = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(raw_servers, list):
            return JSONResponse(status_code=400, content={"detail": 'body must be {"servers": [...]}'})

        problems: List[str] = []
        parsed: List[Any] = []
        seen = set()
        for i, raw in enumerate(raw_servers, 1):
            if not isinstance(raw, dict):
                problems.append(f"entry #{i}: not an object")
                continue
            cfg = _entry(raw, problems)
            if cfg is None:
                continue
            key = cfg.name.lower()
            if key in seen:
                problems.append(f"{cfg.name}: duplicate name (names must be unique)")
                continue
            seen.add(key)
            parsed.append(cfg)

        if problems:
            return JSONResponse(status_code=400, content={"detail": "; ".join(problems)})

        # Preserve every non-"servers" top-level key of the existing file (the human
        # _comment block), then write the validated list back. LF endings by policy;
        # atomic replace so a crash mid-write never leaves a half-written config.
        try:
            doc = {}
            if os.path.exists(SERVERS_FILE):
                with open(SERVERS_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    doc = {k: v for k, v in existing.items() if k != "servers"}
            else:
                doc["_comment"] = [
                    "MCP (Model Context Protocol) servers the CLIENT may expose as tools.",
                    "Managed live from Settings > MCP (POST /api/mcp/servers); you can also",
                    "edit this file directly - it is re-read on every save and at startup.",
                    "NOTHING IS ENABLED BY DEFAULT; enabling a server is a human decision,",
                    "and the first use of its tools still asks a consent card.",
                ]
            doc["servers"] = [_entry_dict(s) for s in parsed]
            tmp = SERVERS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, SERVERS_FILE)
        except (OSError, ValueError, TypeError) as e:
            logger.error(f"[MCP] failed to save mcp_servers.json: {e}")
            return JSONResponse(status_code=500,
                                content={"detail": f"Failed to write config/mcp_servers.json: {e}"})

        await _rewire_registry()

        # Fresh read-back so the UI shows exactly what is on disk + live state now.
        try:
            from app.mcp.registry import get_registry as _get_reg
            reg = _get_reg()
        except Exception:  # pragma: no cover
            reg = None
        servers, problems2 = load_servers()
        logger.info(f"[MCP] settings save: {len(parsed)} server(s), "
                    f"enabled={sorted(s.name for s in parsed if s.enabled) or 'none'}")
        return {"status": "ok",
                "servers": _rows_with_status(servers, reg),
                "problems": problems2,
                "config_file": SERVERS_FILE}
