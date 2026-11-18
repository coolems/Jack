"""Payload transform: our llama-shaped payload -> DeepSeek's /chat/completions body.

DeepSeek accepts the OpenAI surface but has its own reasoning control and ignores/rejects
some llama.cpp keys:

  sent      model, messages, stream, temperature, max_tokens, top_p, tools,
            stream_options (only with stream=true), thinking{type}, reasoning_effort
  dropped   top_k, min_p, presence_penalty, frequency_penalty, chat_template_kwargs
  echoed    reasoning_content on an assistant tool_calls turn (kept when present)

Verified against api.deepseek.com: unknown extra keys are tolerated, but stream_options
without stream=true is a 400, so that key is only added for streaming requests.
"""

from typing import Dict, List, Optional

from config import DEEPSEEK_MAX_NEW_TOKENS, LLAMA_SERVER_TEMP

# Keys our llama payload carries that DeepSeek has no use for.
_DROP_KEYS = ("top_k", "min_p", "presence_penalty", "frequency_penalty",
              "chat_template_kwargs", "seed", "n_probs")

# Internal per-message metadata the CLIENT attaches (see llama/payload.py), plus the
# Ollama-style 'images' key. 'images' is Ollama-only: the streaming/non_streaming paths
# convert it to image_url content BEFORE this cleanup runs; stripping it here is a safety
# net so the raw base64 key never leaks into a DeepSeek body from any other call site.
_INTERNAL_KEYS = ("_est_tokens", "_protect", "_image_paths", "_file_contents", "images")


def _clean_message(msg) -> dict:
    """One message in DeepSeek's shape: internal keys stripped, reasoning_content kept."""
    if not isinstance(msg, dict):
        return msg
    out = {k: v for k, v in msg.items() if k not in _INTERNAL_KEYS}
    # DeepSeek's own reasoning field on an echoed assistant turn is valid input; keep it
    # only there - a reasoning_content on a user or tool message is not part of the API.
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


def build_deepseek_payload(
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
    """Build a DeepSeek /chat/completions body from our request values.

    thinking_body: {"thinking": {...}, "reasoning_effort": "..."} from
    deepseek.thinking.thinking_body(); empty sends no reasoning keys, so the model keeps
    its documented default (thinking on, effort high).
    """
    if temperature is None:
        temperature = LLAMA_SERVER_TEMP
    if max_tokens is None:
        max_tokens = DEEPSEEK_MAX_NEW_TOKENS

    payload: Dict = {
        "model": model,
        "messages": [_clean_message(m) for m in (messages or [])],
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
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
    """Turn an already-built llama payload into the DeepSeek body.

    Used by build_payload() (the relay's payload preview): llama keys are dropped,
    reasoning keys replaced, stream_options kept only for a streaming request.
    """
    body = {k: v for k, v in (llama_payload or {}).items() if k not in _DROP_KEYS}
    body["messages"] = [_clean_message(m) for m in body.get("messages") or []]
    if body.get("stream"):
        body["stream_options"] = {"include_usage": True}
    else:
        body.pop("stream_options", None)
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if thinking_body is not None:
        body.pop("thinking", None)
        body.pop("reasoning_effort", None)
        body.update(thinking_body)
    return body
