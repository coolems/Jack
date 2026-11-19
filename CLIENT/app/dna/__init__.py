"""Agent DNA - CLIENT package shell (thin auto-delegating re-export layer).

The ACTUAL DNA code lives on the SERVER (app/dna/) and is delivered to this
client over WebSocket with tools_response ('shared_sources' -> 'app.dna.*').
This file is intentionally a thin lazy shell: it only defines the package so
that `from app.dna import get_agent` works, then resolves names from the
delivered modules. No DNA logic is duplicated here -- SERVER is the source of
truth, and this shell auto-delegates ANY name to the delivered submodules, so
adding a DNA function or constant on the SERVER never requires editing this file
(the four callables below are explicit only because the loader's
attach_to_parent would otherwise bind their names to the delivered SUBMODULE).

The identity JSON arrives in memory through app.dna.set_dna_data() (installed by
the loader); the CLIENT keeps no DNA file on disk.

CRITICAL: NO FALLBACKS allowed. All agent data comes from SERVER only.
If called before delivery completes, access raises RuntimeError instead of
using hardcoded values.
"""

import logging
import sys

logger = logging.getLogger("COOLEMS.AgentDNA")

# Submodules the SERVER delivers under 'app.dna.*' (see shared_sources.py). A name
# not defined explicitly below is resolved by scanning these in order, so the SERVER
# can add/rename DNA attributes without any change here.
_DELIVERED_SUBMODULES = ("core", "name", "get_agent")


def _delivered_available() -> bool:
    """True once at least one SERVER-delivered DNA submodule is in sys.modules."""
    return any(f"app.dna.{m}" in sys.modules for m in _DELIVERED_SUBMODULES)


def _require_delivery(what: str) -> None:
    if not _delivered_available():
        raise RuntimeError(
            f"{what} unavailable - AgentDNA not yet delivered from SERVER. "
            "Ensure RemoteToolOrchestrator.initialize_from_server() completed first."
        )


# The four callables below are kept explicit (not delegated through __getattr__) so
# `from app.dna import get_agent` binds the FUNCTION, not the delivered submodule
# app.dna.get_agent that the loader attaches to this package's namespace.


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
    """Resolve any other DNA name from the SERVER-delivered submodules.

    Constants (AgentDNA, AGENT_NAME, dna_data, ...) and any name the SERVER adds in
    the future resolve here automatically - no hand-maintained mirror of the SERVER's
    app/dna/__init__.py API surface.
    """
    if name.startswith("_"):
        raise AttributeError(f"module 'app.dna' has no attribute {name!r}")

    _require_delivery(f"app.dna.{name}")
    for sub in _DELIVERED_SUBMODULES:
        mod = sys.modules.get(f"app.dna.{sub}")
        if mod is not None and hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(
        f"module 'app.dna' has no attribute {name!r} "
        "(not exported by any delivered app.dna submodule)"
    )


# Star-import surface only. Explicit `from app.dna import X` and attribute access
# resolve through __getattr__ for names beyond this list.
__all__ = [
    "AgentDNA",
    "get_agent",
    "get_name",
    "set_name",
    "set_dna_data",
    "AGENT_NAME",
]
