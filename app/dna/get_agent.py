"""Agent singleton - one AgentDNA per process."""

from .core import AgentDNA

_agent_instance = None


def get_agent() -> AgentDNA:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = AgentDNA()
    return _agent_instance
