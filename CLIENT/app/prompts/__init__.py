"""Agent Prompts - Entry point (SERVER-delivered, memory only).

The ACTUAL system prompt content is delivered from SERVER via WebSocket during
initialization and stored in memory only — never saved to disk on CLIENT side.
This module provides a runtime-updatable GENERIC_TOOL_PROMPT that existing code
imports normally; it gets populated when the first tools_response arrives.
"""

# Default empty string — will be replaced at runtime by set_system_prompt_from_server()
GENERIC_TOOL_PROMPT: str = ""


def set_system_prompt_from_server(prompt: str) -> None:
    """Update the module-level GENERIC_TOOL_PROMPT from SERVER delivery.

    Called automatically by RemoteToolOrchestrator.initialize_from_server() when
    the first tools_response arrives. Existing code that does
    `from app.prompts import GENERIC_TOOL_PROMPT` will see the updated value.

    Args:
        prompt: The full system prompt string from SERVER (generic + planning + persistent).
    """
    global GENERIC_TOOL_PROMPT
    if prompt and prompt != GENERIC_TOOL_PROMPT:
        GENERIC_TOOL_PROMPT = prompt


# Re-exported for backward compatibility — these are empty placeholders now.
# The real content lives in the SERVER's tools/agent_prompt/ folder and is delivered at runtime.
from .persistent_plan_prompt import PERSISTENT_PLAN_PROMPT  # noqa: F401


def get_time_awareness_instruction() -> str:
    """Get time awareness instruction (delivered from SERVER).

    Returns empty string as placeholder — real implementation on SERVER.
    """
    return ""


def get_agent_system_prompt() -> str:
    """Get the complete agent system prompt.

    Returns the runtime-updated GENERIC_TOOL_PROMPT if available, otherwise empty.
    The SERVER-side version (tools/agent_prompt/get_agent_system_prompt.py) is the source of truth.
    """
    return GENERIC_TOOL_PROMPT


__all__ = [
    'GENERIC_TOOL_PROMPT',
    'PERSISTENT_PLAN_PROMPT',
    'set_system_prompt_from_server',
    'get_time_awareness_instruction',
    'get_agent_system_prompt',
]
