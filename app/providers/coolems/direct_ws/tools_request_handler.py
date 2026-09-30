"""tools_request handler: ship tool definitions + shared modules to the CLIENT.

Moved verbatim from ws_client_handler.py on 2026-09-07 (lines 605-716), incl.
_load_profile_blocked_libs() which resolves the per-profile python_exec
blocked-libs set shipped with tools_response. Only mechanical change: the
tool_scanner import gained one dot of relative depth (. -> ..).
"""

import json

from .log import logger

def _load_profile_blocked_libs(user_info):
    """Load the authenticated profile's python_exec blocked-libs set from config/.

    Returns a list of module names to block on import:
      - []            -> empty admin clean set: NO restrictions at all (allow everything)
      - ["os", ...]   -> only these modules are import-blocked
      - None          -> no per-profile set / unreadable file: the tool falls back to its
                         built-in default blocklist (fail-safe, never fail-open)

    The result is shipped with tools_response so python_exec enforces EXACTLY this
    profile's constraints on the CLIENT where it actually executes.
    """
    if not user_info:
        return None
    filename = user_info.get("python_exec_blocked_libs")
    if not filename:
        logger.info("[SERVER] Profile has no python_exec blocked-libs set -> built-in defaults apply (role=%s)", user_info.get("role"))
        return None
    try:
        from app.auth_gateway.profile_loader import _resolve_config_dir, load_blocked_libs_file
        modules = load_blocked_libs_file(_resolve_config_dir(), filename)
    except Exception as e:
        logger.warning("[SERVER] Could not load blocked-libs set %r: %s -> built-in defaults apply", filename, e)
        return None
    if modules is None:
        logger.warning("[SERVER] Blocked-libs file %r missing/invalid -> built-in defaults apply (fail-safe)", filename)
        return None
    logger.info("[SERVER] python_exec blocked-libs set %r loaded: %d module(s) for role=%s%s",
                filename, len(modules), user_info.get("role"), " (EMPTY = allow everything)" if not modules else "")
    return modules


async def _handle_tools_request(ws, user_info, server_provider):
    """Handle tools_request message - return all allowed tool definitions + shared modules.

    FIX #4 (2026-01-31) — Role Injection into Config Constants:
        The authenticated user's role is injected into config_constants so that
        CLIENT-side tools (running in sandboxed process) can detect the correct
        security level. This solves the problem where server knows admin role but
        client-side python_exec defaults to "user" and blocks everything.

    Flow:
        1. SERVER injects CURRENT_USER_ROLE into config_constants dict
        2. Config constants sent over WebSocket in tools_response JSON
        3. CLIENT dynamic_loader injects config_constants into globals() before exec()
        4. The role is carried as IDENTITY data only -- python_exec no longer
        consumes it for any security decision (its guardrails are driven by the
        per-profile blocked-libs set, see tools/exec_tools/python_exec.py).
    """
    from ..tool_scanner import get_client_tools_dir, scan_tool_definitions, get_shared_utils_source, get_all_shared_sources, extract_config_constants, get_dna_data_files, get_framework_sources, get_shared_hashes

    tools_dir = get_client_tools_dir()
    if not tools_dir:
        await ws.send(json.dumps({
            "type": "tools_response",
            "error": "Tools directory not found on SERVER"
        }))
        return

    definitions, allowed_tools = scan_tool_definitions(tools_dir)

    # Filter by user role permissions
    if user_info and hasattr(server_provider, '_auth_gateway'):
        tool_names = [d.get("function", {}).get("name") for d in definitions]
        filtered_names = server_provider._auth_gateway.get_allowed_tools(user_info, tool_names)
        definitions = [d for d in definitions
                       if d.get("function", {}).get("name") in set(filtered_names)]
        allowed_tools = filtered_names

    # --- Shared modules: send BOTH new dict format AND legacy string format ---
    shared_sources = get_all_shared_sources(tools_dir)  # Dict with all module sources (utils, path_guard, ssrf_defense, web_interact/*)
    shared_utils_legacy = get_shared_utils_source(tools_dir)  # Legacy single-string for old clients

    # FIX (2026-01-31): Extract and send config constants needed by CLIENT-side tools
    config_constants = extract_config_constants()

    # FIX #4 (2026-01-31): Inject authenticated user's role into config_constants.
    # Sent to CLIENT where dynamic_loader injects it into globals() as identity
    # data for tools that need the role. NOTE (2026-08-28): python_exec does NOT
    # read it anymore -- its guardrails are driven by the per-profile blocked-libs
    # set, not by role.
    if user_info:
        config_constants["CURRENT_USER_ROLE"] = user_info.get("role", "user")

    # DNA data files (agent state) -- SERVER is source of truth, delivered to client
    dna_data = get_dna_data_files(tools_dir)

    # SYSTEM PROMPT: SERVER is source of truth — delivered to CLIENT memory only
    from tools.agent_prompt import get_agent_system_prompt
    system_prompt = get_agent_system_prompt()

    # (2026-08-20 dedup) FRAMEWORK modules: base.py + token_stats.py are now delivered
    # from SERVER and installed in-memory on the CLIENT -- no more duplicated copies.
    framework_sources = get_framework_sources(tools_dir)
    # (2026-08-20 integrity) SHA-256 prefixes for every shipped module so the CLIENT can
    # verify each one before exec'ing it (fail-closed when hashes are present).
    shared_hashes = get_shared_hashes(tools_dir)

    logger.info(f"[SERVER->CLIENT] Sending {len(definitions)} tool definitions + {len(shared_sources)} shared modules + {len(config_constants)} config constants + {len(dna_data)} DNA data files + system_prompt (role={config_constants.get('CURRENT_USER_ROLE', 'N/A')}) to authenticated role={user_info.get('role') if user_info else 'unknown'}")
    await ws.send(json.dumps({
        "type": "tools_response",
        "definitions": definitions,
        "allowed_tools": allowed_tools,
        "shared_utils_source": shared_utils_legacy,   # Legacy: backward compat for old clients
        "shared_sources": shared_sources,               # New: dict with ALL shared modules (incl. app.dna.* delivered from SERVER)
        "config_constants": config_constants,           # FIX (2026-01-31): constants + role for web_search etc.
        "python_exec_blocked_libs": _load_profile_blocked_libs(user_info),  # per-profile import blocklist ([]=allow all, None=built-in defaults)
        "framework_sources": framework_sources,         # 2026-08-20: app.providers.base/token_stats -- dedup via delivery
        "shared_hashes": shared_hashes,                 # 2026-08-20: integrity hashes for all shipped modules
        "dna_data": dna_data,                           # DNA JSON state files (identity/intelligence/lessons)
        "system_prompt": system_prompt                  # System prompt — delivered to CLIENT memory only, never saved to disk
    }))
