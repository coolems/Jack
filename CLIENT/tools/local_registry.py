"""local_registry - CLIENT-side MCP seam: definitions + dispatch (2026-12).

The CLIENT's tool schema comes from the SERVER (tools_response). MCP tools are CLIENT-LOCAL
by necessity: they own child processes, outbound endpoints and consent cards on THIS machine,
which a sandboxed worker-thread tool cannot do. This module is the seam that adds them to the
schema and routes their calls. ask_user_question keeps its own seam (tools.local_ask_registry).

Three jobs, nothing else:
  * definitions(role)                 -> OpenAI-shaped dicts merged into the schema the model sees
  * allowed_for_role(name, role)      -> the role gate the SERVER cannot apply to these names
  * call(name, args, context)         -> awaited handler result (never a worker thread)

Role gating is explicit here rather than inherited from the SERVER's per-role allowlist: the
SERVER never sees mcp__* names, so a role that must not reach an external server has to be
refused on the CLIENT. Anything not listed is refused fail-closed.

MCP tool names are dynamic (mcp__<server>__<tool>) and gated per server by
CLIENT/client_config/mcp_servers.json, so an unknown server or an unknown role contributes nothing to
the schema and returns an honest error when called.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("COOLEMS.LocalRegistry")

#: Roles allowed to call the MCP meta-tools. '*' = any authenticated role. These four are
#: read-only introspection (they report what is actually connected); the mcp__<server>__<tool>
#: calls that reach an external process or endpoint are gated per server in mcp_servers.json
#: and still ask a consent card on first use.
LOCAL_TOOL_ROLES: Dict[str, frozenset] = {
    "list_mcp_servers": frozenset({"*", "user", "admin"}),
    "list_mcp_resources": frozenset({"*", "user", "admin"}),
    "list_mcp_resource_templates": frozenset({"*", "user", "admin"}),
    "read_mcp_resource": frozenset({"*", "user", "admin"}),
}


def _mcp_module():
    """Import the MCP tool module lazily and never let a broken install break the schema."""
    try:
        from tools import mcp_local
        return mcp_local
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[LOCAL-TOOLS] mcp_local unavailable: {e}")
        return None


def _mcp_registry():
    try:
        from app.mcp.registry import get_registry
        return get_registry()
    except Exception:
        return None


def is_mcp_tool(name: str) -> bool:
    """True when *name* is an mcp__<server>__<tool> call owned by the registry."""
    try:
        from app.mcp.registry import is_mcp_tool as _is
        return bool(_is(name))
    except Exception:
        return False


def is_local_tool(name: str) -> bool:
    """True when *name* is handled HERE instead of by the SERVER-delivered registry."""
    if name in LOCAL_TOOL_ROLES:
        return True
    mcp = _mcp_module()
    if mcp is not None and name in mcp.MCP_HANDLERS:
        return True
    return is_mcp_tool(name)


def local_tool_names() -> List[str]:
    """Every MCP name this registry can dispatch. An INVENTORY, not the schema.

    Role-independent on purpose: definitions() is what the model sees for ITS role, while
    this list answers "is this name ours?" for permission checks and the settings panel.
    Filtering it by role would hide a server's tools from an admin view.
    """
    names = sorted(LOCAL_TOOL_ROLES)
    mcp = _mcp_module()
    if mcp is not None:
        names.extend(sorted(mcp.MCP_HANDLERS))
        reg = _mcp_registry()
        if reg is not None:
            for client in reg.clients():
                if client.connected:
                    names.extend(t.name for t in client.tools)
    # The meta-tools appear in LOCAL_TOOL_ROLES and in MCP_HANDLERS; the inventory is a set.
    return sorted(set(names))


def allowed_for_role(name: str, role: Optional[str]) -> bool:
    """Role gate for one MCP tool. Fail-closed on an unknown role or unknown server."""
    if is_mcp_tool(name):
        reg = _mcp_registry()
        if reg is None:
            return False
        # An mcp__<server>__<tool> call reaches an EXTERNAL process or endpoint, so the role
        # must be known: the CLIENT has no authoritative role until the SERVER confirms one,
        # and a '*' server entry means "any CONFIRMED role", not "any caller". definitions()
        # applies the same rule, so the schema and the dispatcher never disagree.
        if not role:
            return False
        server, _ = reg.split_name(name)
        client = reg.get(server)
        # Enabled + role only: definitions() additionally filters on connected, so the schema
        # never offers a dead server's tools, while a server that died mid-session still reaches
        # registry.call (which reconnects, or asks for endpoint approval, and reports honestly).
        return bool(client) and client.cfg.enabled and client.cfg.allows_role(role)
    roles = LOCAL_TOOL_ROLES.get(name)
    if roles is None:
        return False
    if "*" in roles:
        return True
    return bool(role) and role in roles


def definitions(role: Optional[str] = None) -> List[Dict]:
    """MCP definitions for the model, filtered by role. Never raises."""
    out: List[Dict] = []
    mcp = _mcp_module()
    if mcp is not None:
        for name, definition in mcp.MCP_TOOL_DEFINITIONS.items():
            if name not in mcp.MCP_HANDLERS:
                continue  # a definition with no handler is a schema lie - skip it
            if allowed_for_role(name, role):
                out.append(definition)
    reg = _mcp_registry()
    if reg is not None:
        try:
            out.extend(reg.definitions(role))
        except Exception as e:
            logger.debug(f"[LOCAL-TOOLS] MCP dynamic definitions failed: {e}")
    return out


def mcp_instructions(role: Optional[str] = None) -> str:
    """Server instructions as untrusted DATA for the tool prompt (empty when none)."""
    reg = _mcp_registry()
    if reg is None:
        return ""
    try:
        return reg.instructions_text(role)
    except Exception:
        return ""


async def call(name: str, arguments: Dict[str, Any], *, websocket,
               conversation_id: str, session_timer=None,
               role: Optional[str] = None) -> Any:
    """Dispatch one MCP call on the event-loop thread. Never leaks a traceback.

    The handler context is the calling turn only (websocket + conversation + timer + role),
    so concurrent chats never see each other's consent cards."""
    ctx = {"websocket": websocket, "conversation_id": conversation_id,
           "session_timer": session_timer, "role": role}

    mcp = _mcp_module()
    if mcp is not None and name in mcp.MCP_HANDLERS:
        if not allowed_for_role(name, role):
            return (f"ERROR: Tool '{name}' is not allowed for your role ({role or 'unknown'}). "
                    "Do not retry it or work around it with another tool.")
        # The context lives exactly as long as this call, so a resource read can ask a
        # consent card on THIS chat and no stale turn context can leak.
        mcp.set_call_context(ctx)
        try:
            result = mcp.MCP_HANDLERS[name](**(arguments or {}))
            if asyncio.iscoroutine(result):
                result = await result
            logger.debug(f"[LOCAL-TOOL] {name} -> {str(result)[:120]}")
            return result
        except asyncio.CancelledError:
            raise   # a user Stop must propagate, not become a tool result
        except Exception as e:
            logger.error(f"[LOCAL-TOOL] {name} failed: {e!r}")
            return f"ERROR: local tool '{name}' failed: {type(e).__name__}: {e}"
        finally:
            mcp.clear_call_context()

    if is_mcp_tool(name):
        # mcp__<server>__<tool>: the registry owns the role gate, consent and dispatch.
        if not allowed_for_role(name, role):
            return (f"ERROR: MCP tool '{name}' is not available for your role "
                    f"({role or 'unknown'}) or its server is not enabled in "
                    "CLIENT/client_config/mcp_servers.json. Do not retry it.")
        reg = _mcp_registry()
        try:
            return await reg.call(name, arguments or {}, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[LOCAL-TOOL] {name} failed: {e!r}")
            return f"ERROR: MCP call '{name}' failed: {type(e).__name__}: {e}"

    return f"ERROR: local tool '{name}' has no handler in this CLIENT build."
