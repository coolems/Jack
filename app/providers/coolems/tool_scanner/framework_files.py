"""Framework module sources delivered to CLIENTs in memory (app.providers.*)."""

import os
from .shared_sources import _read_text

def get_framework_sources(tools_dir):
    """Read the shared FRAMEWORK modules that both sides used to keep as local copies.

    (2026-08-20 dedup) The CLIENT distribution no longer ships disk copies of these;
    they are delivered with every tools_response and installed IN MEMORY only via the
    existing 'app.*' sys.modules mechanism:

      - app.providers.base        -> BaseProvider ABC (the provider contract)
      - app.providers.token_stats -> TokenStats dataclass

    The SERVER keeps its own local copies (it needs them at import time); the CLIENT
    resolves `from app.providers import BaseProvider` through a lazy package shell that
    binds to these delivered modules. This removes the last hand-synced code duplication
    between server and client -- SERVER is now the single source of truth for both sides'
    provider contract, exactly like it already is for tools/dna/prompts.

    app.provider_manager is NOT here: the CLIENT needs a local copy at import time before
    any WS exists (chicken-and-egg); that one stays byte-identical and is covered by a
    sync test instead.
    """
    sources = {}
    providers_dir = os.path.join(os.path.dirname(tools_dir), "app", "providers")
    # ORDER MATTERS (2026-08-20): token_stats MUST be installed before base --
    # base.py does `from .token_stats import TokenStats` at module level, and the
    # client loader installs same-depth modules in dict insertion order. The CLIENT
    # no longer has disk copies to fall back on, so a wrong order = install failure.
    for mod_name in ("token_stats", "base"):
        path = os.path.join(providers_dir, f"{mod_name}.py")
        if os.path.exists(path):
            sources[f'app.providers.{mod_name}'] = _read_text(path)
    return sources
