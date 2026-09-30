"""Get agent singleton function"""

from .core import AgentDNA
from .learning import LessonManager

_agent_instance = None

def get_agent() -> AgentDNA:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = AgentDNA()
        _agent_instance.lesson_manager = LessonManager(_agent_instance.dna_dir)
    return _agent_instance