"""Payload builder for llama.cpp /v1/chat/completions requests.

Sampling defaults (top_p, top_k, min_p, presence_penalty) come from config.py - change
them there, not here.
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
    thinking_kwargs: Dict = None,
) -> Dict:
    """Build an OpenAI-compatible /v1/chat/completions payload.

    thinking_kwargs: chat_template_kwargs for reasoning control, already translated to this
    model's vocabulary by ThinkingPolicy. payload.py never invents a kwarg - a strict
    template raises on an undeclared variable - so empty/None sends no thinking keys.
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

    # ---- Reasoning control ----
    # Only what ThinkingPolicy produced, verbatim. An empty dict is deliberately not added:
    # llama.cpp/Strata ignore unknown keys, but a strict GGUF template raises on an
    # undeclared template variable, so a model without thinking knobs must get no keys.
    if thinking_kwargs:
        payload["chat_template_kwargs"] = dict(thinking_kwargs)

    # ---- Token Usage in SSE Chunks ----
    # Always request usage data from llama.cpp for streaming responses.
    # This enables accurate per-request token stats (prompt_tokens, completion_tokens)
    # captured from the final SSE chunk's 'usage' field. Without this, streaming.py
    # falls back to rough character-based estimates (len(text) // 4).
    if stream:
        payload["stream_options"] = {"include_usage": True}

    return payload
