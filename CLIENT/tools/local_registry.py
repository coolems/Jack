"""local_registry — CLIENT-side tool definitions + dispatch (2026-12).

The CLIENT's tool schema comes from the SERVER (tools_response). Subagent / job / question
tools and MCP tools are CLIENT-LOCAL by necessity: they own asyncio tasks, channels, consent
cards and child processes on THIS machine, which a sandboxed worker-thread tool cannot do.
This module is the seam that adds them to the schema and routes their calls.

Three jobs, nothing else:
  * definitions(role)  -> OpenAI-shaped dicts merged into the schema the model sees
  * allowed_for_role(name, role) -> the role gate the SERVER cannot apply to these names
  * call(name, args, context) -> awaited handler result (never a worker thread)

Role gating is explicit here rather than inherited from the SERVER's per-role allowlist:
the SERVER never sees these names, so a role that must not delegate or must not reach an
external server has to be refused on the CLIENT. Anything not listed is refused fail-closed.

MCP tool names are dynamic (mcp__<server>__<tool>) and gated per server by
CLIENT/config/mcp_servers.json, so an unknown server or an unknown role contributes
nothing to the schema and returns an honest error when called.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from tools.local_tools import TOOL_DEFINITIONS, TOOL_HANDLERS, clear_local_context, set_local_context

logger = logging.getLogger("COOLEMS.LocalRegistry")

#: Roles allowed to call each fixed CLIENT-local tool. '*' = any authenticated role. The
#: CLIENT has no authoritative role until the SERVER confirms one (app.keys.get_role falls
#: back to 'user'), so the default set covers 'user' too — delegation is a capability, not
#: a secret. MCP meta-tools are read-only introspection; the mcp__<server>__<tool> calls
#: that actually reach an external process are gated per server in mcp_servers.json.
LOCAL_TOOL_ROLES: Dict[str, frozenset] = {
    "subagent": frozenset({"*", "user", "admin"}),
    "subagent_fork": frozenset({"*", "user", "admin"}),
    "send_message": frozenset({"*", "user", "admin"}),
    "interrupt_agent": frozenset({"*", "user", "admin"}),
    "list_agents": frozenset({"*", "user", "admin"}),
    "job_list": frozenset({"*", "user", "admin"}),
    "job_output": frozenset({"*", "user", "admin"}),
    "job_kill": frozenset({"*", "user", "admin"}),
    "ask_user_question": frozenset({"*", "user", "admin"}),
    "list_mcp_servers": frozenset({"*", "user", "admin"}),
    "list_mcp_resources": frozenset({"*", "user", "admin"}),
    "list_mcp_resource_templates": frozenset({"*", "user", "admin"}),
    "read_mcp_resource": frozenset({"*", "user", "admin"}),
}


def _mcp_module():
    """Import the MCP seam lazily and never let a broken install break the schema."""
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


def is_local_tool(name: str) -> bool:
    """True when *name* is handled HERE instead of by the SERVER-delivered registry."""
    if name in TOOL_HANDLERS:
        return True
    mcp = _mcp_module()
    if mcp is not None:
        if name in mcp.MCP_HANDLERS:
            return True
        from app.mcp.registry import is_mcp_tool
        return is_mcp_tool(name)
    return False


def local_tool_names() -> List[str]:
    """Every name this registry can dispatch. An INVENTORY, not the schema.

    The inventory is role-independent on purpose: definitions() is what the model sees for
    ITS role, while this list answers "is this name ours?" for permission checks and the
    runtime panel. Filtering it by role would hide a server's tools from an admin view.
    """
    names = sorted(TOOL_HANDLERS)
    mcp = _mcp_module()
    if mcp is not None:
        names.extend(sorted(mcp.MCP_HANDLERS))
        reg = _mcp_registry()
        if reg is not None:
            for client in reg.clients():
                if client.connected:
                    names.extend(t.name for t in client.tools)
    return names


def allowed_for_role(name: str, role: Optional[str]) -> bool:
    """Role gate for one local tool. Fail-closed on an unknown role or unknown server."""
    from app.mcp.registry import is_mcp_tool
    if is_mcp_tool(name):
        reg = _mcp_registry()
        if reg is None:
            return False
        # An mcp__<server>__<tool> call reaches an EXTERNAL process, so the role must be
        # known: the CLIENT has no authoritative role until the SERVER confirms one, and a
        # '*' server entry means "any CONFIRMED role", not "any caller". definitions()
        # applies the same rule, so the schema and the dispatcher never disagree.
        if not role:
            return False
        server, _ = reg.split_name(name)
        client = reg.get(server)
        # Enabled + role only. `definitions()` additionally filters on connected, so the
        # schema never offers a dead server's tools; at dispatch time a server that died
        # mid-session must reach registry.call (which reconnects and reports honestly)
        # rather than be refused with a misleading role message.
        return bool(client) and client.cfg.enabled and client.cfg.allows_role(role)
    roles = LOCAL_TOOL_ROLES.get(name)
    if roles is None:
        return False
    if "*" in roles:
        return True
    return bool(role) and role in roles


def definitions(role: Optional[str] = None) -> List[Dict]:
    """Definitions for the model, filtered by role. Never raises."""
    out: List[Dict] = []
    for name, definition in TOOL_DEFINITIONS.items():
        if name not in TOOL_HANDLERS:
            continue  # a definition with no handler is a schema lie - skip it
        if allowed_for_role(name, role):
            out.append(definition)

    mcp = _mcp_module()
    if mcp is not None:
        for name, definition in mcp.MCP_TOOL_DEFINITIONS.items():
            if name not in mcp.MCP_HANDLERS:
                continue
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


async def call(name: str, arguments: Dict[str, Any], *, loop, channel, websocket,
               conversation_id: str, ollama_tools: Optional[List[Dict]] = None,
               generic_tool_prompt: str = "", enable_thinking: Any = False,
               temperature: float = 0.6, parent_history: Optional[List[Dict]] = None,
               session_timer=None, role: Optional[str] = None) -> Any:
    """Dispatch one local tool call on the event-loop thread.

    The context is installed for exactly the duration of the call (set/clear in a
    finally), so concurrent chats never see each other's channel or schema. A handler
    error becomes an honest error string — the loop already turns a raised exception into
    a tool result, but a local tool must never leak a traceback into history.
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        mcp = _mcp_module()
        if mcp is not None and name in mcp.MCP_HANDLERS:
            handler = mcp.MCP_HANDLERS[name]
        elif mcp is not None and is_local_tool(name):
            # mcp__<server>__<tool>: the registry owns the role gate, consent and dispatch.
            if not allowed_for_role(name, role):
                return (f"ERROR: MCP tool '{name}' is not available for your role "
                        f"({role or 'unknown'}) or its server is not enabled in "
                        "CLIENT/config/mcp_servers.json. Do not retry it.")
            set_local_context(loop=loop, channel=channel, websocket=websocket,
                              conversation_id=conversation_id, ollama_tools=ollama_tools,
                              generic_tool_prompt=generic_tool_prompt,
                              enable_thinking=enable_thinking, temperature=temperature,
                              parent_history=parent_history, session_timer=session_timer,
                              role=role)
            try:
                return await _mcp_registry().call(name, arguments or {}, _mcp_context())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[LOCAL-TOOL] {name} failed: {e!r}")
                return f"ERROR: MCP call '{name}' failed: {type(e).__name__}: {e}"
            finally:
                clear_local_context()
        return f"ERROR: local tool '{name}' has no handler in this CLIENT build."
    if not allowed_for_role(name, role):
        return (f"ERROR: Tool '{name}' is not allowed for your role ({role or 'unknown'}). "
                "Do not retry it or work around it with another tool.")

    set_local_context(loop=loop, channel=channel, websocket=websocket,
                      conversation_id=conversation_id, ollama_tools=ollama_tools,
                      generic_tool_prompt=generic_tool_prompt,
                      enable_thinking=enable_thinking, temperature=temperature,
                      parent_history=parent_history, session_timer=session_timer,
                      role=role)
    try:
        result = handler(**(arguments or {}))
        if asyncio.iscoroutine(result):
            result = await result
        logger.debug(f"[LOCAL-TOOL] {name} executed")
        return result
    except TypeError as e:
        logger.error(f"[LOCAL-TOOL] {name} bad arguments: {e}")
        return f"ERROR: invalid arguments for '{name}': {e}"
    except asyncio.CancelledError:
        raise   # a user Stop must propagate, not become a tool result
    except Exception as e:
        logger.error(f"[LOCAL-TOOL] {name} failed: {e!r}")
        return f"ERROR: local tool '{name}' failed: {type(e).__name__}: {e}"
    finally:
        clear_local_context()


def _mcp_context() -> Dict[str, Any]:
    """The installed context, read by the MCP dispatch path."""
    from tools.local_tools import _ctx
    return _ctx()
