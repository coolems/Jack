"""System prompt on the CLIENT - delivered text, memory only.

The SERVER composes the prompt strings (identity + tool contract + plan-log rules)
and ships them in tools_response: 'system_prompt' for tool turns, 'chat_prompt'
for plain chat. This module only stores those strings for the request path; the
CLIENT builds no prompt content of its own and never writes it to disk. Empty
until delivery completes.
"""

import logging

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


__all__ = ["set_system_prompt_from_server", "get_system_prompt", "get_chat_prompt"]
