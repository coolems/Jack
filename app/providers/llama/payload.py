"""
Payload builder for llama.cpp /v1/chat/completions requests.

OPTIMIZED FOR QWEN3.6 — Based on official Qwen3.6 recommendations from Unsloth/Qwen docs:
- top_p (nucleus sampling): 0.8 — Official default for general tasks
- top_k: 20 — Official default (limits token selection to top-k candidates)
- min_p: 0.0 — Official default (disabled minimum probability ratio filter)
- presence_penalty: 0.0 — Disabled to prevent workflow abandonment during multi-step tasks

ALL DEFAULT PARAMETERS ARE CENTRALIZED IN config.py — modify there, not here.
"""
from typing import List, Dict

from config import (
    LLAMA_SERVER_MAX_NEW_TOKENS,
    LLAMA_SERVER_TEMP,
)


def _message_text_of(m: Dict) -> str:
    """Best-effort plain-text view of a message's content (str or vision parts list)."""
    content = m.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return str(content or "")


def _coalesce_system_messages(messages: List[Dict]) -> List[Dict]:
    """Merge ALL system-role messages into ONE leading block (template-safe shape).

    Strict chat templates baked into GGUF files (Qwen3.x, Llama 3/4, Gemma 3) raise
    when a system message is not at position 0 or appears more than once. The client
    sends [system(persona), system(turn-boundary marker), user(...)] which Qwen3.6
    tolerates but Qwen3.5 rejects with 'System message must be at the beginning' -
    every request, even a bare "hi", fails at template render time (HTTP 500).

    A single leading system block is valid for EVERY template: lenient ones
    (Qwen2.x, Mistral) accept it identically. So this runs unconditionally with no
    per-model branching. Text content is preserved verbatim - only the container
    changes. Non-system messages keep their exact order and keys.

    Returns the original list unchanged when there is exactly one system message at
    index 0 (the canonical shape - even if blank) or when no non-blank system text
    exists to merge. Blank entries are only dropped on the merge path, where keeping
    them would create a second system message.
    """
    if not messages:
        return messages

    def _is_system(m) -> bool:
        return isinstance(m, dict) and str(m.get("role", "")).lower() == "system"

    system_msgs = [m for m in messages if _is_system(m)]

    # Already canonical: exactly one system message, sitting at index 0 - even a blank
    # one is still ONE leading system, valid for every template.
    if len(system_msgs) == 1 and messages[0] is system_msgs[0]:
        return messages

    system_texts = []
    rest = []
    for m in messages:
        if _is_system(m):
            text = _message_text_of(m)
            if text.strip():
                system_texts.append(text)
            # Empty/whitespace-only system messages are dropped - no template can render them.
        else:
            rest.append(m)

    if not system_texts:
        return messages  # only empty system entries existed - leave the list untouched

    merged = "\n\n".join(system_texts)
    return [{"role": "system", "content": merged}] + rest


def build_llama_payload(
    model: str,
    messages: List[Dict],
    *,
    stream: bool = True,
    temperature: float = None,
    tools: list = None,
    max_tokens: int = None,
    top_p: float = 0.8,
    top_k: int = 20,
    min_p: float = 0.0,
    presence_penalty: float = 0.0,
) -> Dict:
    """
    Build an OpenAI-compatible /v1/chat/completions payload with official
    Qwen3.6 recommended sampling parameters.

    Args:
        model: Model name/ID to use.
        messages: List of message dicts with 'role' and 'content'.
        stream: Whether to stream the response.
        temperature: Sampling temperature (0.0-2.0). Defaults to config LLAMA_SERVER_TEMP.
        tools: Optional list of tool definitions for native tool calling.
        max_tokens: Maximum tokens to generate. Defaults to config LLAMA_SERVER_MAX_NEW_TOKENS.
        top_p: Nucleus sampling threshold (0.0-1.0, default 0.8 per Qwen3.6 docs).
        top_k: Top-k sampling limit (>=0, default 20 per Qwen3.6 docs).
        min_p: Minimum probability ratio (0.0-1.0, default 0.0 per Qwen3.6 docs).
        presence_penalty: Penalty for repeated tokens (0.0-2.0, default 0.0 to prevent workflow abandonment).

    Returns:
        Dict ready to send as JSON to /v1/chat/completions.
    """
    # Use config defaults if not explicitly overridden
    if temperature is None:
        temperature = LLAMA_SERVER_TEMP
    if max_tokens is None:
        max_tokens = LLAMA_SERVER_MAX_NEW_TOKENS

    # Strip internal metadata keys before the messages leave the process. The client
    # caches per-message token counts on the dicts (`_est_tokens`), tags the current
    # user prompt with `_protect`, marks the turn-boundary line with `_loop_marker`, and
    # re-attaches `_image_paths` / `_file_contents`; none of that belongs in the payload.
    _INTERNAL_KEYS = ("_est_tokens", "_protect", "_loop_marker",
                      "_image_paths", "_file_contents")
    messages = [
        {k: v for k, v in m.items() if k not in _INTERNAL_KEYS} if isinstance(m, dict) else m
        for m in (messages or [])
    ]

    # TEMPLATE-SAFE SHAPE (2026-11): merge every system message into ONE leading block.
    # Strict GGUF templates (Qwen3.x, Llama 3/4, Gemma 3) raise 'System message must be
    # at the beginning' on our [system(persona), system(turn-boundary), user(...)] list;
    # a single leading system block is valid for every template, so this is unconditional.
    messages = _coalesce_system_messages(messages)

    payload: Dict = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # ---- Sampling parameters optimized for reliable task completion ----
        "top_p": top_p,
        "top_k": top_k,
        "min_p": min_p,
        "presence_penalty": presence_penalty,
    }

    if tools:
        payload["tools"] = tools

    # ---- Token Usage in SSE Chunks ----
    # Always request usage data from llama.cpp for streaming responses.
    # This enables accurate per-request token stats (prompt_tokens, completion_tokens)
    # captured from the final SSE chunk's 'usage' field. Without this, streaming.py
    # falls back to rough character-based estimates (len(text) // 4).
    if stream:
        payload["stream_options"] = {"include_usage": True}

    return payload
