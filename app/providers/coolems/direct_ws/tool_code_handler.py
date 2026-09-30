"""tool_code_request handler: ship source for a single tool (+ deps + hash).

Moved verbatim from ws_client_handler.py on 2026-09-07 (lines 719-771).
Only mechanical change: the tool_scanner imports gained one dot of depth.
"""

import json

from .log import logger

async def _handle_tool_code_request(ws, msg, user_info, server_provider):
    """Handle tool code request message - return source for a single tool."""
    from ..tool_scanner import get_client_tools_dir, get_tool_source_code_with_deps

    tool_name = msg.get("name", "")

    if not tool_name:
        await ws.send(json.dumps({
            "type": "tool_code_response",
            "error": "No tool name specified"
        }))
        return

    tools_dir = get_client_tools_dir()
    if not tools_dir:
        await ws.send(json.dumps({
            "type": "tool_code_response",
            "error": "Tools directory not found on SERVER"
        }))
        return

    # Check permissions
    if user_info and hasattr(server_provider, '_auth_gateway'):
        allowed = server_provider._auth_gateway.get_allowed_tools(user_info, [tool_name])
        if tool_name not in allowed:
            logger.warning(f"[SERVER] Tool code request denied for {tool_name} (role={user_info.get('role')})")
            await ws.send(json.dumps({
                "type": "tool_code_response",
                "error": f"Tool '{tool_name}' is not available for your role."
            }))
            return

    result = get_tool_source_code_with_deps(tools_dir, tool_name)
    if not result["source_code"]:
        await ws.send(json.dumps({
            "type": "tool_code_response",
            "error": f"Tool '{tool_name}' not found on SERVER"
        }))
        return

    logger.info(f"[SERVER->CLIENT] Sending source for tool '{tool_name}' ({len(result['source_code'])} chars)")
    if result.get("shared_deps"):
        dep_count = len(result.get("shared_deps", {}))
        logger.info(f"[SERVER->CLIENT] Including {dep_count} shared deps for tool")

    # (2026-08-20 integrity) ship the hash so the CLIENT verifies before exec'ing.
    # (2026-08-23) Self-unpacking tools: ship the manifest so the CLIENT unfolds its own runtime.
    _manifest = result.get("manifest")

    from ..tool_scanner import _sha_prefix
    await ws.send(json.dumps({
        "type": "tool_code_response",
        "source_code": result["source_code"],
        "shared_deps": result.get("shared_deps", {}),
        "source_hash": _sha_prefix(result["source_code"]),
        **({"manifest": _manifest} if _manifest else {})
    }))
