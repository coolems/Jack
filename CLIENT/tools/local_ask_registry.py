"""local_ask_registry - CLIENT-side definition + dispatch for ask_user_question.

The tool schema comes from the SERVER, but ask_user_question is CLIENT-LOCAL: it owns a
card and an asyncio Future on this machine. This seam adds it to the schema and routes
its calls - definitions(role), allowed_for_role(name, role), call(name, args, **context).
The SERVER never sees these names, so anything not listed is refused fail-closed."""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from tools.ask_user_question import TOOL_NAME, ask_user_question, question_definition

logger = logging.getLogger("COOLEMS.LocalRegistry")

#: Roles allowed to call each CLIENT-local tool. '*' = any authenticated role. The
#: CLIENT has no authoritative role until the SERVER confirms one (app.keys.get_role falls
#: back to 'user'), so the default set covers 'user' too - asking the human is a
#: capability, not a secret.
LOCAL_TOOL_ROLES: Dict[str, frozenset] = {
    TOOL_NAME: frozenset({"*", "user", "admin"}),
}

#: name -> handler, the inventory this registry can dispatch.
TOOL_HANDLERS: Dict[str, Any] = {TOOL_NAME: ask_user_question}


def is_local_tool(name: str) -> bool:
    """True when *name* is handled HERE instead of by the SERVER-delivered registry."""
    return name in TOOL_HANDLERS


def local_tool_names() -> List[str]:
    """Every name this registry can dispatch. An INVENTORY, not the schema."""
    return sorted(TOOL_HANDLERS)


def allowed_for_role(name: str, role: Optional[str]) -> bool:
    """Role gate for one local tool. Fail-closed on an unknown role or unknown name."""
    roles = LOCAL_TOOL_ROLES.get(name)
    if roles is None:
        return False
    if "*" in roles:
        return True
    return bool(role) and role in roles


def definitions(role: Optional[str] = None) -> List[Dict]:
    """Definitions for the model, filtered by role. Never raises."""
    out: List[Dict] = []
    if allowed_for_role(TOOL_NAME, role):
        out.append(question_definition())
    return out


async def call(name: str, arguments: Dict[str, Any], *, channel, websocket,
               conversation_id: str, session_timer=None,
               role: Optional[str] = None) -> Any:
    """Dispatch one local tool call on the event-loop thread.

    The handler gets the turn's context explicitly, so concurrent chats never see each
    other's channel. A handler error becomes an honest error string."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"ERROR: local tool '{name}' has no handler in this CLIENT build."
    if not allowed_for_role(name, role):
        return (f"ERROR: Tool '{name}' is not allowed for your role ({role or 'unknown'}). "
                "Do not retry it or work around it with another tool.")

    ctx = {"channel": channel, "websocket": websocket,
           "conversation_id": conversation_id, "session_timer": session_timer,
           "role": role}
    try:
        result = handler(**(arguments or {}), ctx=ctx)
        if asyncio.iscoroutine(result):
            result = await result
        logger.debug(f"[LOCAL-TOOL] {name} -> {str(result)[:120]}")
        return result
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[LOCAL-TOOL] {name} failed: {e!r}")
        return f"ERROR: local tool '{name}' failed: {type(e).__name__}: {e}"
