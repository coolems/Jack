"""Agent DNA - Entry point"""

from .core import AgentDNA
from .get_agent import get_agent
from .name import get_name, set_name, AGENT_NAME
from .learning import LessonManager

__all__ = [
    'AgentDNA',
    'get_agent',
    'get_name',
    'set_name',
    'AGENT_NAME',
    'LessonManager',
]