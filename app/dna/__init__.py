"""Agent DNA - the agent's identity (name, personality, values, rules, goals).

Not a tool, so it lives in app/dna/ - outside tools/. SERVER is the source of
truth (app/dna/identity.json). These modules are delivered to CLIENTs as code in
tools_response ('shared_sources' -> 'app.dna.*'); a client keeps no DNA file on
disk - the identity JSON arrives in memory through set_dna_data().

DNA is read-only: the learning/lessons pipeline was removed, identity text is
the only thing this package produces.
"""

from .core import AgentDNA, dna_data, set_dna_data, set_dna_dir
from .get_agent import get_agent
from .name import AGENT_NAME, get_name, set_name

__all__ = [
    "AgentDNA",
    "get_agent",
    "get_name",
    "set_name",
    "AGENT_NAME",
    "set_dna_data",
    "set_dna_dir",
    "dna_data",
]
