"""Agent DNA - CLIENT package shell (thin re-export layer).

The ACTUAL DNA code lives on the SERVER (app/dna/) and is delivered to this
client over WebSocket with tools_response ('shared_sources' -> 'app.dna.*').
This file is intentionally a thin lazy shell: it only defines the package so
that `from app.dna import get_agent` works, then pulls names from the delivered
modules. No DNA logic may be duplicated here -- SERVER is the source of truth.

The identity JSON arrives in memory through app.dna.set_dna_data() (installed by
the loader); the CLIENT keeps no DNA file on disk.

CRITICAL: NO FALLBACKS allowed. All agent data comes from SERVER only.
If called before delivery completes, functions raise RuntimeError instead of
using hardcoded values.
"""

import logging

logger = logging.getLogger("COOLEMS.AgentDNA")


def _delivered_available() -> bool:
    """True once the SERVER-delivered DNA submodules are registered in sys.modules."""
    try:
        from . import core  # noqa: F401 -- resolves only if delivered module is installed
        return True
    except Exception as e:
        logger.debug("Delivered DNA modules not available yet (%s)", type(e).__name__)
        return False


def _require_delivery(what: str) -> None:
    if not _delivered_available():
        raise RuntimeError(
            f"{what} unavailable - AgentDNA not yet delivered from SERVER. "
            "Ensure RemoteToolOrchestrator.initialize_from_server() completed first."
        )


def get_agent():
    """Return the agent singleton from SERVER-delivered code."""
    _require_delivery("app.dna.get_agent")
    from .get_agent import get_agent as _delivered_get_agent  # type: ignore
    return _delivered_get_agent()


def set_dna_data(mapping) -> None:
    """Install the SERVER-shipped identity JSON in memory (loader entry point).

    Called by tools/remote_tools/loader/shared_modules.py right after the DNA
    modules are installed. The payload stays in memory only - the CLIENT keeps no
    DNA file on disk, so this is the only way identity data reaches a client.
    """
    _require_delivery("app.dna.set_dna_data")
    from .core import set_dna_data as _delivered_set_dna_data  # type: ignore
    _delivered_set_dna_data(mapping)


def get_name() -> str:
    """Agent name from SERVER-delivered DNA."""
    _require_delivery("app.dna.get_name")
    from .name import get_name as _delivered_get_name  # type: ignore
    return _delivered_get_name()


def set_name(new_name: str) -> None:
    """Set the agent name in SERVER-delivered DNA."""
    _require_delivery("app.dna.set_name")
    from .name import set_name as _delivered_set_name  # type: ignore
    _delivered_set_name(new_name)


def __getattr__(name):
    """Lazy attribute access for AgentDNA / AGENT_NAME from delivered modules."""
    if name in ("AgentDNA", "AGENT_NAME"):
        _require_delivery(f"app.dna.{name}")
        if name == "AgentDNA":
            from .core import AgentDNA  # type: ignore
            return AgentDNA
        from .name import AGENT_NAME  # type: ignore
        return AGENT_NAME
    raise AttributeError(f"module 'app.dna' has no attribute {name!r}")


__all__ = [
    "AgentDNA",
    "get_agent",
    "get_name",
    "set_name",
    "set_dna_data",
    "AGENT_NAME",
]
