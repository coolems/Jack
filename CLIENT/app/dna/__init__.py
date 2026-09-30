"""Agent DNA - CLIENT package shell (thin re-export layer).

The ACTUAL DNA code lives on the SERVER (tools/dna/) and is delivered to this
client over WebSocket with tools_response ('shared_sources' -> 'app.dna.*').
This file is intentionally a thin lazy shell: it only defines the package so
that `from app.dna import get_agent` works, then pulls names from the delivered
modules. No DNA logic may be duplicated here -- SERVER is the source of truth.

CRITICAL (2026-08): NO FALLBACKS allowed. All agent data comes from SERVER only.
If called before delivery completes, functions will raise RuntimeError instead
of using hardcoded values. This ensures identity/intelligence/lessons always
come from SERVER (source of truth), same as system prompts.
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


def get_agent():
    """Return the agent singleton from SERVER-delivered code.

    Raises RuntimeError until SERVER delivers DNA via WebSocket. No fallback allowed.
    This ensures identity data always comes from SERVER (source of truth).
    """
    if not _delivered_available():
        raise RuntimeError(
            "AgentDNA not yet delivered from SERVER. "
            "Ensure RemoteToolOrchestrator.initialize_from_server() completed first."
        )

    from .get_agent import get_agent as _delivered_get_agent  # type: ignore
    return _delivered_get_agent()


def get_name() -> str:
    """Get agent name from SERVER-delivered DNA."""
    if not _delivered_available():
        raise RuntimeError(
            "AgentDNA not yet delivered from SERVER. "
            "Ensure RemoteToolOrchestrator.initialize_from_server() completed first."
        )
    from .name import get_name as _delivered_get_name  # type: ignore
    return _delivered_get_name()


def set_name(new_name: str) -> None:
    """Set agent name in SERVER-delivered DNA."""
    if not _delivered_available():
        raise RuntimeError(
            "AgentDNA not yet delivered from SERVER. "
            "Ensure RemoteToolOrchestrator.initialize_from_server() completed first."
        )
    from .name import set_name as _delivered_set_name  # type: ignore
    _delivered_set_name(new_name)


def __getattr__(name):
    """Lazy attribute access for AgentDNA / LessonManager from delivered modules."""
    if name in ("AgentDNA", "LessonManager", "AGENT_NAME"):
        if not _delivered_available():
            raise RuntimeError(
                f"Cannot access {name!r} - AgentDNA not yet delivered from SERVER. "
                "Ensure RemoteToolOrchestrator.initialize_from_server() completed first."
            )
        if name == "AgentDNA":
            from .core import AgentDNA  # type: ignore
            return AgentDNA
        if name == "LessonManager":
            from .learning import LessonManager  # type: ignore
            return LessonManager
        from .name import AGENT_NAME  # type: ignore
        return AGENT_NAME
    raise AttributeError(f"module 'app.dna' has no attribute {name!r}")


__all__ = [
    "AgentDNA",
    "get_agent",
    "get_name",
    "set_name",
    "AGENT_NAME",
    "LessonManager",
]
