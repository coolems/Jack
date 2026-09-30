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
    # user prompt with `_protect`, and re-attaches `_image_paths` / `_file_contents`;
    # none of that belongs in the llama.cpp payload.
    _INTERNAL_KEYS = ("_est_tokens", "_protect", "_image_paths", "_file_contents")
    messages = [
        {k: v for k, v in m.items() if k not in _INTERNAL_KEYS} if isinstance(m, dict) else m
        for m in (messages or [])
    ]

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
