"""Provider factory — SERVER ONLY.

Exposes only providers needed for coolems_server operation:
  - CoolemsServerProvider: WebSocket brain server (headless relay to local LLM)
  - LlamaProvider         : Local llama.cpp backend (used internally by CoolemsServerProvider)
  - StrataProvider        : Local Strata endpoint (SERVER_BACKEND = "strata")
  - DeepSeekProvider      : Hosted DeepSeek API (SERVER_BACKEND = "deepseek")
  - OpenAIProvider        : Hosted OpenAI API (SERVER_BACKEND = "openai")
"""
from .base import BaseProvider

__all__ = [
    "BaseProvider",
    "LlamaProvider",
    "StrataProvider",
    "DeepSeekProvider",
    "OpenAIProvider",
    "CoolemsServerProvider",
    "get_provider",
]


def get_provider(provider_name: str, api_url: str, timeout: int) -> BaseProvider:
    """Factory function — server-side providers only.

    Supported (server):
      - 'llama':           llama.cpp server (local backend)
      - 'strata':          Strata inference endpoint (same wire format, Bearer
                           auth, attach-only - the brain never spawns )
      - 'deepseek':        DeepSeek's hosted OpenAI-compatible API (Bearer auth,
                           attach-only; request/response transformed to DeepSeek's shape)
      - 'openai':          OpenAI's hosted API (Bearer auth, attach-only; request/response
                           transformed to OpenAI's shape, reasoning via reasoning_effort)
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
    elif provider_name == "deepseek":
        from .deepseek.provider import DeepSeekProvider
        return DeepSeekProvider(api_url, timeout)
    elif provider_name == "openai":
        from .openai.provider import OpenAIProvider
        return OpenAIProvider(api_url, timeout)
    elif provider_name == "coolems_server":
        from .coolems.server_provider import CoolemsServerProvider
        return CoolemsServerProvider(api_url, timeout)
    else:
        raise ValueError(
            f"Unknown server provider: {provider_name}. "
            f"Valid server providers: 'llama', 'strata', 'deepseek', 'openai', 'coolems_server'. "
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
    elif name == "DeepSeekProvider":
        from .deepseek.provider import DeepSeekProvider as _DSP
        return _DSP
    elif name == "OpenAIProvider":
        from .openai.provider import OpenAIProvider as _OAP
        return _OAP
    elif name == "CoolemsServerProvider":
        from .coolems.server_provider import CoolemsServerProvider as _CSP
        return _CSP
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
