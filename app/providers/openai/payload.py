"""Payload transform: our llama-shaped payload -> OpenAI's /chat/completions body.

The request SHAPE comes from openai/wire_profiles.py (one row per model family): the
output-ceiling key name, whether sampling params are accepted, and the reasoning key.

  sent      model, messages, stream, temperature (classic families only),
            max_tokens / max_completion_tokens (per profile.token_key),
            top_p (classic families only), tools, stream_options (only with stream=true),
            reasoning_effort (reasoning families, always top level)
  dropped   top_k, min_p, presence_penalty, frequency_penalty, chat_template_kwargs,
            temperature + top_p for reasoning families (they reject sampling params)
  echoed    reasoning_content on an assistant tool_calls turn (kept when present)

Verified against api.openai.com: unknown extra keys are tolerated, but reasoning
families 400 on temperature/top_p and on a nested text.format body.
"""

from typing import Dict, List, Optional

from config import LLAMA_SERVER_TEMP, OPENAI_MAX_NEW_TOKENS

from .wire_profiles import TOOLS_CHAT_NO_REASONING, is_reasoning, profile_for

# Keys our llama payload carries that OpenAI has no use for.
_DROP_KEYS = ("top_k", "min_p", "presence_penalty", "frequency_penalty",
              "chat_template_kwargs", "seed", "n_probs")

# Internal per-message metadata the CLIENT attaches (see llama/payload.py), plus the
# Ollama-style 'images' key. 'images' is Ollama-only: the streaming/non_streaming paths
# convert it to image_url content BEFORE this cleanup runs; stripping it here is a safety
# net so the raw base64 key never leaks into an OpenAI body from any other call site.
_INTERNAL_KEYS = ("_est_tokens", "_protect", "_image_paths", "_file_contents", "images")


def _clean_message(msg) -> dict:
    """One message in OpenAI's shape: internal keys stripped, reasoning_content kept."""
    if not isinstance(msg, dict):
        return msg
    out = {k: v for k, v in msg.items() if k not in _INTERNAL_KEYS}
    # reasoning_content on an echoed assistant turn is valid input; keep it only there -
    # a reasoning_content on a user or tool message is not part of the API.
    if out.get("role") != "assistant":
        out.pop("reasoning_content", None)
        out.pop("tool_calls", None)
        return out
    content = out.get("content")
    if content is None and out.get("tool_calls"):
        out["content"] = ""
    if not out.get("tool_calls"):
        out.pop("tool_calls", None)
    if not content and not out.get("tool_calls"):
        out.pop("content", None)
    return out


def _is_reasoning_model(model: str) -> bool:
    """True for families that reject sampling params and need max_completion_tokens.

    Delegates to openai.wire_profiles: the family table is the single source of truth.
    """
    return is_reasoning(model)


def build_openai_payload(
    model: str,
    messages: List[Dict],
    *,
    stream: bool = True,
    temperature: float = None,
    tools: list = None,
    max_tokens: int = None,
    top_p: float = None,
    thinking_body: Dict = None,
) -> Dict:
    """Build an OpenAI /chat/completions body from our request values.

    thinking_body: {"reasoning_effort": "..."} (o-series, gpt-5, gpt-6) from
    openai.thinking.thinking_body(); empty sends no reasoning keys, so the model keeps
    its documented default.
    """
    if max_tokens is None:
        max_tokens = OPENAI_MAX_NEW_TOKENS

    profile = profile_for(model)
    payload: Dict = {
        "model": model,
        "messages": [_clean_message(m) for m in (messages or [])],
        "stream": stream,
    }
    # The family's wire profile names the output-ceiling key (gpt-5/gpt-6/o-series
    # reject 'max_tokens'; they take max_completion_tokens).
    payload[profile.token_key] = max_tokens
    if profile.sampling_params:
        if temperature is None:
            temperature = LLAMA_SERVER_TEMP
        payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
    if tools:
        payload["tools"] = tools
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if thinking_body:
        payload.update(thinking_body)
    # gpt-5.4/gpt-5.5 400 on "Function tools with reasoning_effort are not supported":
    # with tools present they must not carry an explicit reasoning_effort.
    if tools and profile.tools_mode == TOOLS_CHAT_NO_REASONING:
        payload.pop("reasoning_effort", None)
    return payload


def transform_llama_payload(llama_payload: Dict, *,
                            thinking_body: Optional[Dict] = None,
                            max_tokens: Optional[int] = None) -> Dict:
    """Turn an already-built llama payload into the OpenAI body.

    Used by build_payload() (the relay's payload preview): llama keys are dropped,
    sampling keys dropped for o-series models, reasoning keys replaced, stream_options
    kept only for a streaming request.
    """
    body = {k: v for k, v in (llama_payload or {}).items() if k not in _DROP_KEYS}
    body["messages"] = [_clean_message(m) for m in body.get("messages") or []]
    profile = profile_for(str(body.get("model") or ""))
    if not profile.sampling_params:
        body.pop("temperature", None)
        body.pop("top_p", None)
    if profile.token_key != "max_tokens" and "max_tokens" in body:
        body[profile.token_key] = body.pop("max_tokens")
    if body.get("stream"):
        body["stream_options"] = {"include_usage": True}
    else:
        body.pop("stream_options", None)
    if max_tokens is not None:
        body[profile.token_key] = max_tokens
    if thinking_body is not None:
        body.pop("reasoning_effort", None)
        body.pop("text", None)
        body.update(thinking_body)
    if body.get("tools") and profile.tools_mode == TOOLS_CHAT_NO_REASONING:
        body.pop("reasoning_effort", None)
    return body
