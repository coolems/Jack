"""
Provider factory — SERVER ONLY.

Exposes only providers needed for coolems_server operation:
  - CoolemsServerProvider : WebSocket brain server (headless relay to local LLM)
  - LlamaProvider         : Local llama.cpp backend (used internally by CoolemsServerProvider)
  - StrataProvider        : Local Strata endpoint (2026-11, SERVER_BACKEND = "strata")
"""
from .base import BaseProvider

__all__ = [
    "BaseProvider",
    "LlamaProvider",
    "StrataProvider",
    "CoolemsServerProvider",
    "get_provider",
]


def get_provider(provider_name: str, api_url: str, timeout: int) -> BaseProvider:
    """Factory function — server-side providers only.

    Supported (server):
      - 'llama':           llama.cpp server (local backend)
      - 'strata':          Strata inference endpoint (2026-11; same wire format, Bearer
                           auth, attach-only - the brain never spawns Strata)
      - 'coolems_server':  Headless WebSocket brain relay

    Client providers (coolems_client) live in CLIENT/ folder.
    """
    provider_name = provider_name.lower().strip()

    if provider_name == "llama":
        from .llama.provider import LlamaProvider
        return LlamaProvider(api_url, timeout)
    elif provider_name == "strata":
        from .strata.provider import StrataProvider
        return StrataProvider(api_url, timeout)
    elif provider_name == "coolems_server":
        from .coolems.server_provider import CoolemsServerProvider
        return CoolemsServerProvider(api_url, timeout)
    else:
        raise ValueError(
            f"Unknown server provider: {provider_name}. "
            f"Valid server providers: 'llama', 'strata', 'coolems_server'. "
            f"For client providers (coolems_client), use CLIENT/ folder."
        )


# PEP 562 lazy module-level imports for backward compatibility
def __getattr__(name):
    if name == "LlamaProvider":
        from .llama.provider import LlamaProvider as _LP
        return _LP
    elif name == "StrataProvider":
        from .strata.provider import StrataProvider as _SP
        return _SP
    elif name == "CoolemsServerProvider":
        from .coolems.server_provider import CoolemsServerProvider as _CSP
        return _CSP
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
