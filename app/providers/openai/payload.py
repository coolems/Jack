"""Payload transform: our llama-shaped payload -> OpenAI's /chat/completions body.

OpenAI accepts the OpenAI surface but rejects some llama.cpp keys:

  sent      model, messages, stream, temperature (non-reasoning models only),
            max_tokens, top_p, tools, stream_options (only with stream=true),
            reasoning_effort / text.format.reasoning_effort (reasoning models)
  dropped   top_k, min_p, presence_penalty, frequency_penalty, chat_template_kwargs,
            temperature + top_p for o-series models (they reject sampling params)
  echoed    reasoning_content on an assistant tool_calls turn (kept when present)

Verified against api.openai.com: unknown extra keys are tolerated, but o-series models
400 on temperature/top_p, so those keys are dropped for them.
"""

from typing import Dict, List, Optional

from config import LLAMA_SERVER_TEMP, OPENAI_MAX_NEW_TOKENS

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
    """True for families that reject sampling params (o-series)."""
    mid = (model or "").lower()
    return mid.startswith(("o1", "o3", "o4"))


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

    thinking_body: {"reasoning_effort": "..."} (o-series) or
    {"text": {"format": {"type": "text", "reasoning_effort": "..."}}} (gpt-5) from
    openai.thinking.thinking_body(); empty sends no reasoning keys, so the model keeps
    its documented default.
    """
    if max_tokens is None:
        max_tokens = OPENAI_MAX_NEW_TOKENS

    payload: Dict = {
        "model": model,
        "messages": [_clean_message(m) for m in (messages or [])],
        "stream": stream,
        "max_tokens": max_tokens,
    }
    if not _is_reasoning_model(model):
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
    if _is_reasoning_model(str(body.get("model") or "")):
        body.pop("temperature", None)
        body.pop("top_p", None)
    if body.get("stream"):
        body["stream_options"] = {"include_usage": True}
    else:
        body.pop("stream_options", None)
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if thinking_body is not None:
        body.pop("reasoning_effort", None)
        body.pop("text", None)
        body.update(thinking_body)
    return body
