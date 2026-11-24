"""System prompt on the CLIENT - delivered text, memory only.

The SERVER composes the prompt strings (identity + tool contract + plan-log rules)
and ships them in tools_response: 'system_prompt' for tool turns, 'chat_prompt'
for plain chat. This module only stores those strings for the request path; the
CLIENT supplies workspace context per tool turn and never writes prompts to disk.
Empty until delivery completes.
"""

import logging
import re

logger = logging.getLogger("COOLEMS.Prompts")

_SYSTEM_PROMPT: str = ""
_CHAT_PROMPT: str = ""


def set_system_prompt_from_server(system_prompt: str, chat_prompt: str = "") -> None:
    """Store the SERVER-delivered prompts in memory."""
    global _SYSTEM_PROMPT, _CHAT_PROMPT
    if system_prompt and system_prompt != _SYSTEM_PROMPT:
        _SYSTEM_PROMPT = system_prompt
        logger.info("System prompt from SERVER stored (%d chars)", len(system_prompt))
    if chat_prompt and chat_prompt != _CHAT_PROMPT:
        _CHAT_PROMPT = chat_prompt
        logger.info("Chat prompt from SERVER stored (%d chars)", len(chat_prompt))


def get_system_prompt() -> str:
    """Prompt for tool turns ('' before delivery)."""
    return _SYSTEM_PROMPT


def get_chat_prompt() -> str:
    """Prompt for plain chat turns ('' before delivery)."""
    return _CHAT_PROMPT


def with_workspace_working_root(prompt: str, working_root: str) -> str:
    """Add the same turn-local root that the tools will use, including with older servers."""
    prompt = re.sub(r"^Your working_root \(default file operations folder\) is:[^\n]*\n?", "", prompt, flags=re.MULTILINE)
    prompt = re.sub(
        r"^## IMPORTANT - YOUR ROOT FOLDER\r?\n- Your root folder is dynamically set in your identity[^\n]*"
        r"(?:\r?\n- [^\n]*)*",
        "", prompt, flags=re.MULTILINE,
    )
    return (
        f"{prompt.rstrip()}\n\n## WORKSPACE FOR THIS TURN\n"
        f"Your working_root (default file operations folder) is: {working_root}\n"
        "Tools already start in this folder; use an empty directory argument for the root. "
        "Use relative subdirectories without prefixing them with the workspace folder name.\n"
    ).lstrip()


__all__ = ["set_system_prompt_from_server", "get_system_prompt", "get_chat_prompt", "with_workspace_working_root"]
