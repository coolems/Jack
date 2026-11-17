"""System-prompt builder - the SERVER composes the prompt strings it ships.

The CLIENT no longer assembles prompts: tools_response carries finished strings.
DNA is the injectable slot - the caller passes whatever identity text it wants in
front of the contract (SERVER: AgentDNA.get_identity()).
"""

from .generic_tool_prompt import GENERIC_TOOL_PROMPT
from .persistent_plan_prompt import PERSISTENT_PLAN_PROMPT

# Identity lock - kept out of the tool contract so plain chat stays tool-free.
_RESPONSE_RULES = """## RESPONSE RULES
- You are the agent named above. This is your identity.
- Never introduce yourself as anything else.
"""


def build_system_prompt(dna_content: str = "", include_tools: bool = True) -> str:
    """Identity block + (tool contract + plan-log rules) + identity lock.

    Args:
        dna_content: Agent identity text (name, working_root, personality, values,
            rules, goals). Empty keeps the prompt identity-neutral.
        include_tools: False builds the plain-chat prompt (no tool contract, no
            planning protocol) for turns that have no tools available.
    """
    dna = (dna_content or "").strip()
    parts = [dna]
    if include_tools:
        parts.append(GENERIC_TOOL_PROMPT.strip())
        parts.append(PERSISTENT_PLAN_PROMPT.strip())
    parts.append(_RESPONSE_RULES.strip())
    return "\n\n".join(p for p in parts if p) + "\n"


def build_chat_prompt(dna_content: str = "") -> str:
    """Plain-chat prompt: identity + identity lock, no tool contract."""
    return build_system_prompt(dna_content, include_tools=False)


def get_agent_system_prompt() -> str:
    """Tool contract + plan-log rules only (no identity block)."""
    return build_system_prompt("")
