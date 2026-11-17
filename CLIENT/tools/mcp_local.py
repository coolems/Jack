"""mcp_local — the four MCP meta-tools the model always sees (2026-12).

Server tools themselves are dynamic (`mcp__<server>__<tool>`, contributed by the registry
from each connected server). These four are fixed and CLIENT-local: they describe what is
actually connected instead of letting the model guess a server name.

WHY CLIENT-LOCAL: the registry lives on this machine (config file + child processes), and
the SERVER never sees mcp__* names. These are awaited on the event loop by
tools/local_registry.py, not run in a sandboxed worker thread.

The calling turn's context (websocket, conversation id, timer, role) is installed by
local_registry around the call via set_call_context(), so a resource read can ask a consent
card on THIS chat and the role gate matches the schema exactly.
"""

import json
import logging
import threading
from typing import Any, Dict

logger = logging.getLogger("COOLEMS.MCP.LocalTools")

#: The turn's context, installed for the duration of one call (see set_call_context).
_context_local = threading.local()


def set_call_context(ctx: Dict[str, Any]) -> None:
    """Install the calling turn's context for the duration of one MCP meta-tool call."""
    _context_local.context = ctx if isinstance(ctx, dict) else {}


def clear_call_context() -> None:
    """Drop it, so a stale turn's websocket/role can never leak into the next call."""
    _context_local.context = {}


def _err(msg: str) -> str:
    return f"ERROR: {msg}"


def _registry_or_error():
    from app.mcp.registry import get_registry
    reg = get_registry()
    if reg is None:
        return None, _err(
            "no MCP servers are enabled in this CLIENT. Enable one in "
            "CLIENT/config/mcp_servers.json (the user's decision) - do not invent a server.")
    return reg, ""


async def list_mcp_servers() -> str:
    """Every configured MCP server: connected or not, and why."""
    reg, err = _registry_or_error()
    if reg is None:
        return err
    rows = reg.status()
    if not rows:
        return "No MCP servers are enabled. Add one to CLIENT/config/mcp_servers.json."
    return json.dumps({"servers": rows, "problems": reg.problems}, ensure_ascii=False, indent=2)


async def list_mcp_resources(server: str = "") -> str:
    """Resources offered by one server (or all connected servers when omitted)."""
    reg, err = _registry_or_error()
    if reg is None:
        return err
    ctx = _ctx()
    role = ctx.get("role")
    denied = _role_gate(role)
    if denied:
        return denied
    out: Dict[str, Any] = {}
    for client in reg.clients():
        if server and client.cfg.name != server:
            continue
        if not client.cfg.allows_role(role):
            continue
        if not client.connected:
            out[client.cfg.name] = {"error": client.error or "not connected"}
        else:
            out[client.cfg.name] = client.resources
    if server and server not in out:
        return _err(f"MCP server '{server}' is not enabled/allowed here. Call list_mcp_servers.")
    if not out:
        return "No connected MCP servers expose resources for your role."
    return json.dumps(out, ensure_ascii=False, indent=2)


async def list_mcp_resource_templates(server: str = "") -> str:
    """Templated resources (uriTemplates) - the shape to build a uri from."""
    reg, err = _registry_or_error()
    if reg is None:
        return err
    ctx = _ctx()
    role = ctx.get("role")
    denied = _role_gate(role)
    if denied:
        return denied
    out: Dict[str, Any] = {}
    for client in reg.clients():
        if server and client.cfg.name != server:
            continue
        if not client.cfg.allows_role(role):
            continue
        if not client.connected:
            out[client.cfg.name] = {"error": client.error or "not connected"}
        else:
            out[client.cfg.name] = client.templates
    if server and server not in out:
        return _err(f"MCP server '{server}' is not enabled/allowed here. Call list_mcp_servers.")
    if not out:
        return "No connected MCP servers expose resource templates for your role."
    return json.dumps(out, ensure_ascii=False, indent=2)


async def read_mcp_resource(server: str, uri: str) -> str:
    """Read one resource by exact server name + uri. Binary payloads become a file path."""
    reg, err = _registry_or_error()
    if reg is None:
        return err
    if not server or not uri:
        return _err("both server and uri are required - use list_mcp_resources to get exact uris.")
    return await reg.read_resource(server, uri, _ctx())


def _ctx() -> Dict[str, Any]:
    """The calling turn's context, installed by tools.local_registry around the call."""
    ctx = getattr(_context_local, "context", None)
    return ctx if isinstance(ctx, dict) else {}


def _role_gate(role) -> str:
    """Error text when this turn has no confirmed role, else ''.

    A resource list is content from an EXTERNAL server (a stdio filesystem server reports
    paths of this machine), so it follows the same rule as registry.read_resource(): with no
    confirmed role nothing from an external server is handed over. list_mcp_servers stays
    role-independent on purpose - it reports this CLIENT's own config state, which the
    Settings panel shows anyway, and the model needs it to name a server."""
    if not role:
        return _err("this turn has no confirmed role, so no external MCP server content is "
                    "available. Do not retry it or route around it.")
    return ""


MCP_TOOL_DEFINITIONS: Dict[str, Dict] = {
    "list_mcp_servers": {
        "type": "function",
        "function": {
            "name": "list_mcp_servers",
            "description": (
                "List the MCP servers configured in this CLIENT: enabled, connected, tool "
                "count, resource count, and the failure reason for any that did not connect. "
                "Call this before using an mcp__<server>__<tool> name so you never guess one."),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    "list_mcp_resources": {
        "type": "function",
        "function": {
            "name": "list_mcp_resources",
            "description": (
                "List resources exposed by an MCP server (omit 'server' for all connected ones). "
                "Reading a resource's content requires read_mcp_resource."),
            "parameters": {"type": "object", "properties": {
                "server": {"type": "string", "description": "Exact server name, or empty for all."}},
                "required": []},
        },
    },
    "list_mcp_resource_templates": {
        "type": "function",
        "function": {
            "name": "list_mcp_resource_templates",
            "description": (
                "List templated resources (uriTemplates) of an MCP server, so you can build a "
                "uri to read with read_mcp_resource."),
            "parameters": {"type": "object", "properties": {
                "server": {"type": "string", "description": "Exact server name, or empty for all."}},
                "required": []},
        },
    },
    "read_mcp_resource": {
        "type": "function",
        "function": {
            "name": "read_mcp_resource",
            "description": (
                "Read one MCP resource by exact server name + uri. Text is returned (oversized "
                "payloads spill to a file path); binary payloads are saved to a file and never "
                "placed in the transcript."),
            "parameters": {"type": "object", "properties": {
                "server": {"type": "string", "description": "Exact server name from list_mcp_servers."},
                "uri": {"type": "string", "description": "Exact resource uri from list_mcp_resources."}},
                "required": ["server", "uri"]},
        },
    },
}


MCP_HANDLERS: Dict[str, Any] = {
    "list_mcp_servers": list_mcp_servers,
    "list_mcp_resources": list_mcp_resources,
    "list_mcp_resource_templates": list_mcp_resource_templates,
    "read_mcp_resource": read_mcp_resource,
}
