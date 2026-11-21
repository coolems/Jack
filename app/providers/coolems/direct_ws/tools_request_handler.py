"""tools_request handler: ship tool definitions + shared modules to the CLIENT.

_load_profile_blocked_libs resolves the per-profile python_exec blocked-libs set shipped
with tools_response.
"""

import json
import os

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
        The authenticated user's role is injected into config_constants so that CLIENT-side
        tools (running in sandboxed process) can detect the correct security level. This
        solves the problem where server knows admin role but client-side python_exec
        defaults to "user" and blocks everything.
    """
    from ..tool_scanner import get_client_tools_dir, scan_tool_definitions, build_tool_catalog, get_shared_utils_source, get_all_shared_sources, extract_config_constants, get_dna_data_files, get_framework_sources, get_shared_hashes

    tools_dir = get_client_tools_dir()
    if not tools_dir:
        await ws.send(json.dumps({
            "type": "tools_response",
            "error": "Tools directory not found on SERVER"
        }))
        return

    definitions, allowed_tools, categories = scan_tool_definitions(tools_dir)

    # Filter by user role permissions
    if user_info and hasattr(server_provider, '_auth_gateway'):
        tool_names = [d.get("function", {}).get("name") for d in definitions]
        filtered_names = server_provider._auth_gateway.get_allowed_tools(user_info, tool_names)
        _allowed_set = set(filtered_names)
        definitions = [d for d in definitions
                       if d.get("function", {}).get("name") in _allowed_set]
        # Keep the category map consistent with the filtered definitions so the
        # CLIENT never advertises (or starts) a tool the role cannot use.
        categories = {name: folder for name, folder in categories.items()
                      if name in _allowed_set}
        allowed_tools = filtered_names

    # Compact catalog (built AFTER role filtering so it never advertises a tool the
    # role cannot use). Ships with the response; the CLIENT appends it to the prompt
    # so the model can discover tools without paying for their full JSON schema.
    tool_catalog = build_tool_catalog(definitions, categories)

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

    # DNA identity JSON (agent state) -- SERVER is source of truth, delivered to client
    # in memory; the CLIENT keeps no DNA file on disk.
    dna_data = get_dna_data_files(os.path.dirname(tools_dir))

    # SYSTEM PROMPT: the SERVER composes the prompt strings here (DNA slot + tool
    # contract) and ships the finished text; the CLIENT stores it and never
    # assembles a prompt of its own. Two variants: tool turns get the tool
    # contract, plain chat gets identity only (no tool schema, no planning rules).
    from app.prompts import build_chat_prompt, build_system_prompt
    from app.dna import get_agent
    dna_text = get_agent().get_identity()
    system_prompt = build_system_prompt(dna_text)
    chat_prompt = build_chat_prompt(dna_text)

    # (2026-08-20 dedup) FRAMEWORK modules: base.py + token_stats.py are now delivered
    # from SERVER and installed in-memory on the CLIENT -- no more duplicated copies.
    framework_sources = get_framework_sources(tools_dir)
    # (2026-08-20 integrity) SHA-256 prefixes for every shipped module so the CLIENT can
    # verify each one before exec'ing it (fail-closed when hashes are present).
    shared_hashes = get_shared_hashes(tools_dir)

    logger.info(f"[SERVER->CLIENT] Sending {len(definitions)} tool definitions (+catalog {len(tool_catalog)} chars, {len(categories)} categories) + {len(shared_sources)} shared modules + {len(config_constants)} config constants + {len(dna_data)} DNA data files + system_prompt ({len(system_prompt)} chars) + chat_prompt ({len(chat_prompt)} chars) (role={config_constants.get('CURRENT_USER_ROLE', 'N/A')}) to authenticated role={user_info.get('role') if user_info else 'unknown'}")
    await ws.send(json.dumps({
        "type": "tools_response",
        "definitions": definitions,
        "allowed_tools": allowed_tools,
        "tool_categories": categories,                 # tool name -> folder (intent-gating + catalog)
        "tool_catalog": tool_catalog,                  # compact grouped text index for the prompt
        "shared_utils_source": shared_utils_legacy,   # Legacy: backward compat for old clients
        "shared_sources": shared_sources,               # New: dict with ALL shared modules (incl. app.dna.* delivered from SERVER)
        "config_constants": config_constants,           # FIX (2026-01-31): constants + role for web_search etc.
        "python_exec_blocked_libs": _load_profile_blocked_libs(user_info),  # per-profile import blocklist ([]=allow all, None=built-in defaults)
        "framework_sources": framework_sources,         # 2026-08-20: app.providers.base/token_stats -- dedup via delivery
        "shared_hashes": shared_hashes,                 # 2026-08-20: integrity hashes for all shipped modules
        "dna_data": dna_data,                           # DNA identity JSON - CLIENT memory only, never written to disk
        "system_prompt": system_prompt,                 # Finished tool-turn prompt (DNA + tool contract) - CLIENT memory only
        "chat_prompt": chat_prompt                      # Plain-chat prompt (DNA only) - CLIENT memory only
    }))
