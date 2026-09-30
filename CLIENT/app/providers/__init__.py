"""
COOLEMS Client - Provider package (LAZY SHELL).

(2026-08-20 dedup) This used to be a plain package containing local copies of
base.py (BaseProvider ABC) and token_stats.py (TokenStats dataclass) that had to
be hand-synced with the SERVER's app/providers/ modules. Those disk copies are GONE:

  - The SERVER now delivers both modules with every tools_response under
    'framework_sources' ('app.providers.base', 'app.providers.token_stats').
  - DynamicModuleLoader installs them IN MEMORY only (sys.modules), never on disk.
  - This shell resolves names lazily from the delivered modules, so
    `from app.providers import BaseProvider` keeps working exactly as before.

Boot order guarantee: RemoteToolOrchestrator.initialize_from_server() runs during
init_provider_and_app() BEFORE get_provider() is called and BEFORE uvicorn starts,
so by the time any code subclasses BaseProvider or reads TokenStats the delivered
modules are already in sys.modules.

Pre-delivery safety net: if something imports this package before delivery has run
(e.g. a module-level annotation evaluated at import time), __getattr__ returns a
clear pending placeholder for BaseProvider (annotations only -- never used for
isinstance/subclassing pre-init) and raises an actionable RuntimeError for anything
else. There is NO local fallback copy by design: the SERVER is the single source of
truth for the provider contract, same as it already is for tools/DNA/prompts.

app.provider_manager intentionally keeps its local disk copy (chicken-and-egg: it is
imported at boot before any WS exists) and stays byte-identical with the SERVER's --
enforced by tests/test_protocol_sync_20260820.py.
"""

import logging
import sys

logger = logging.getLogger("COOLEMS.Provider.Factory")


def _delivered_module(name: str):
    """Return the SERVER-delivered module for 'app.providers.<name>' if installed."""
    return sys.modules.get(f"app.providers.{name}")


class _PendingBaseProvider:
    """Placeholder returned ONLY before delivery completes.

    Exists so that import-time annotation evaluation (e.g. `provider: BaseProvider = None`
    in function signatures of modules imported during boot) does not crash. It is NOT a
    real ABC and must never be used for isinstance() or subclassing -- all such uses happen
    at runtime, long after delivery has installed the real delivered class.
    """

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "_PendingBaseProvider is a pre-delivery placeholder. "
            "The real BaseProvider is delivered from SERVER with tools_response -- "
            "this object must never be instantiated or subclassed."
        )

    def __getattr__(self, item):
        # Any attribute use before delivery (e.g. reading a class constant) fails loudly here
        # instead of silently behaving like an empty object.
        raise RuntimeError(
            f"Access to {type(self).__name__}.{item} before SERVER delivery. "
            "The real framework module arrives with tools_response -- check startup order."
        )


def get_provider(provider_name: str, api_url: str, timeout: int):
    """Factory function for CLIENT-side providers only.

    ONLY supports 'coolems_client' (WebSocket connection to SERVER).
    Direct LLM access is BLOCKED — the SERVER relay is the only path.

    The actual provider class is imported lazily so that this package can be
    imported at boot time BEFORE the SERVER delivers app.providers.base.
    """
    if _delivered_module("base") is None:
        raise RuntimeError(
            "app.providers.base has not been delivered from SERVER yet -- cannot build a provider. "
            "Ensure RemoteToolOrchestrator.initialize_from_server() ran first (init_provider_and_app does this)."
        )

    if provider_name.lower().strip() != "coolems_client":
        raise ValueError(
            f"CLIENT cannot use provider '{provider_name}'. "
            f"It must connect to SERVER via 'coolems_client' (WebSocket relay). "
            f"To start a local UI with direct LLM access, run code.py in the SERVER root folder."
        )

    from .coolems.client_provider import CoolemsClientProvider
    return CoolemsClientProvider(api_url or "", timeout)


def __getattr__(name):
    """PEP 562 lazy attribute resolution for delivered framework modules."""
    if name == "BaseProvider":
        mod = _delivered_module("base")
        if mod is not None:
            return mod.BaseProvider
        logger.warning(
            "[PROVIDERS] BaseProvider requested before SERVER delivery -- returning pending placeholder. "
            "This is only safe for import-time annotations."
        )
        return _PendingBaseProvider

    if name == "TokenStats":
        mod = _delivered_module("token_stats")
        if mod is not None:
            return mod.TokenStats
        # Pre-delivery placeholder (same contract as BaseProvider): import-time annotation
        # binding in app.websocket.message_types must succeed before delivery runs; any real
        # use (instantiation / attribute access) raises loudly until the SERVER delivers it.
        logger.warning(
            "[PROVIDERS] TokenStats requested before SERVER delivery -- returning pending placeholder."
        )

        class _PendingTokenStats(_PendingBaseProvider):
            pass

        return _PendingTokenStats

    if name == "CoolemsClientProvider":
        from .coolems.client_provider import CoolemsClientProvider
        return CoolemsClientProvider

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseProvider",
    "TokenStats",
    "CoolemsClientProvider",
    "get_provider",
]
