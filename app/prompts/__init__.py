"""Agent prompts - SERVER source of truth for the system prompt.

Not tools, so they live in app/prompts/ (outside tools/). The SERVER builds the
final system_prompt here and ships it in tools_response; the CLIENT stores that
string in memory and never composes a prompt of its own.
"""

from .builder import build_chat_prompt, build_system_prompt, get_agent_system_prompt
from .generic_tool_prompt import GENERIC_TOOL_PROMPT
from .get_time_awareness_instruction import get_time_awareness_instruction
from .persistent_plan_prompt import PERSISTENT_PLAN_PROMPT
from .planning_prompt import PLANNING_PROMPT

__all__ = [
    "build_system_prompt",
    "build_chat_prompt",
    "get_agent_system_prompt",
    "get_time_awareness_instruction",
    "GENERIC_TOOL_PROMPT",
    "PLANNING_PROMPT",
    "PERSISTENT_PLAN_PROMPT",
]
