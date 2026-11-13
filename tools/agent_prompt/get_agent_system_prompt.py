"""Get agent system prompt function - combines tool descriptions with planning prompt"""

from .generic_tool_prompt import GENERIC_TOOL_PROMPT
from .persistent_plan_prompt import PERSISTENT_PLAN_PROMPT


def get_agent_system_prompt() -> str:
    """Get the complete agent system prompt with dynamic tool descriptions"""
    # No tool descriptions needed - Ollama handles tools natively
    return GENERIC_TOOL_PROMPT.replace("{{TOOL_DESCRIPTIONS}}", "") + PERSISTENT_PLAN_PROMPT