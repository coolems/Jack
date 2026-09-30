"""Agent Prompts - Entry point

This module provides all agent system prompts and tool descriptions.
"""

from .generic_tool_prompt import GENERIC_TOOL_PROMPT
from .persistent_plan_prompt import PERSISTENT_PLAN_PROMPT
from .get_time_awareness_instruction import get_time_awareness_instruction
from .get_agent_system_prompt import get_agent_system_prompt

# build_tool_descriptions and get_tool_count are REMOVED - no longer needed

__all__ = [
    'GENERIC_TOOL_PROMPT',
    'PERSISTENT_PLAN_PROMPT',
    'get_time_awareness_instruction',
    'get_agent_system_prompt',
]