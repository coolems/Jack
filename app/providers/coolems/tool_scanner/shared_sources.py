"""Shared tools/ module sources shipped to CLIENTs (multi-module delivery)."""

import os

from .log import logger

def _read_text(path):
    """Read a text file as UTF-8 (shared by the source-serving functions)."""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def get_shared_utils_source(tools_dir):

    """Read the shared utils.py source code for CLIENT sandbox.

    LEGACY — kept for backward compatibility with old clients.
    Newer clients use get_all_shared_sources() instead.
    """

    utils_path = os.path.join(tools_dir, "utils.py")

    if os.path.exists(utils_path):

        return _read_text(utils_path)


    return None

def get_all_shared_sources(tools_dir):
    """Read ALL shared module sources needed by CLIENT tools.

    Returns a dict mapping module keys to source code strings.
    Keys use dot notation for subpackages (e.g., 'web_interact.shared_instance').

    This replaces the legacy single-string shared_utils_source with a proper
    multi-module approach so that subpackage modules (WebInteract, shared_instance)
    are available on CLIENT via sys.modules['tools.web_interact'] etc.

    Order of installation matters for circular dependencies:
      1. Core top-level modules first (utils, path_guard, ssrf_defense)
      2. Subpackage __init__.py files second
      3. Subpackage child modules last

    The CLIENT's install_shared_modules() handles ordering internally.
    """
    sources = {}

    # --- Core shared modules (top-level in tools/) ---
    # ORDER MATTERS (2026-09): ssrf_guard BEFORE ssrf_defense -- the shim imports
    # from it at exec time on the CLIENT, and same-depth keys install in send order.
    for mod_name in ('utils', 'path_guard', 'ssrf_guard', 'ssrf_defense', 'tool_bootstrap'):
        path = os.path.join(tools_dir, f"{mod_name}.py")
        if os.path.exists(path):
            sources[mod_name] = _read_text(path)

    # --- web_interact subpackage shared modules ---
    wi_dir = os.path.join(tools_dir, "web_interact")

    if os.path.isdir(wi_dir):
        init_path = os.path.join(wi_dir, "__init__.py")
        if os.path.exists(init_path):
            sources['web_interact'] = _read_text(init_path)

        si_path = os.path.join(wi_dir, "shared_instance.py")
        if os.path.exists(si_path):
            sources['web_interact.shared_instance'] = _read_text(si_path)

    # --- provider_manager: delivered so TOOL code doing 'from app.provider_manager
    # import ProviderManager' resolves to server-served source on the client. The
    # CLIENT also keeps a local copy (required at boot before any WS exists) -- see
    # the DELIVERY NOTE in that file; both copies must stay byte-identical. ---
    pm_path = os.path.join(os.path.dirname(tools_dir), "app", "provider_manager.py")
    if os.path.exists(pm_path):
        sources['app.provider_manager'] = _read_text(pm_path)

    # --- DNA (agent identity/learning) -- SERVER is the source of truth for code.
    # Delivered to each client so it can use its agent locally; NO .py copies may
    # exist in the CLIENT distribution. Keys are registered on the client as
    # 'app.dna.*' (the client's import path). Order matters: dependencies first
    # (name, learning before core; get_agent last). __init__ is NOT delivered --
    # the client keeps a thin lazy package shell for that name.
    dna_dir = os.path.join(tools_dir, "dna")
    if os.path.isdir(dna_dir):
        for mod_name in ('name', 'learning', 'core', 'get_agent'):
            path = os.path.join(dna_dir, f"{mod_name}.py")
            if os.path.exists(path):
                sources[f'app.dna.{mod_name}'] = _read_text(path)

    return sources
