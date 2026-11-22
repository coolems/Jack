"""OpenAI /v1/responses support for families that reject tools in /chat/completions.

gpt-5.6 and gpt-6 (and their dated/-sol/-luna/-astra/-terra variants) cannot combine
function tools with reasoning in /v1/chat/completions at all: an explicit
reasoning_effort 400s, omitting it still 400s (the model's default reasoning is
non-disableable), and reasoning_effort='none' is not an accepted value. OpenAI's own
error says to use /v1/responses instead, so tool requests for those families are routed
here. Non-tool requests keep the normal /chat/completions path.

The /v1/responses request/response shapes differ from chat completions:

  input     chat messages are translated to responses items: system -> 'instructions',
            assistant tool_calls -> function_call items, tool role -> function_call_output,
            everything else -> role/content message items
  tools     chat 'function' objects are flattened to top-level name/description/parameters
  output    function_call items -> the same {"type":"tool_calls",...} JSON the chat path
            returns; message items -> concatenated output_text
  usage     input_tokens/output_tokens map onto prompt/generated tokens

This is deliberately NON-streaming: tool calls are never streamed incrementally by the
chat path anyway, and a single content frame for a plain answer matches the client's
contract. Vision content is converted to input_text/input_image parts on a best-effort
basis (the tool flow is text-first).
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import (
    CONTEXT_WINDOW_TOKENS,
    LLAMA_MIN_SAFE_MAX_TOKENS,
    OPENAI_MAX_NEW_TOKENS,
)

from ..llama.http_client import get_async_client
from ..token_stats import TokenStats

logger = logging.getLogger("COOLEMS.Provider.OpenAI.Responses")


def _content_text(content: Any) -> str:
    """Best-effort text extraction from a chat message content (str or list of parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return str(content)


def _convert_content(content: Any) -> Any:
    """Chat content -> /v1/responses content (str stays str, parts get renamed)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text"):
                parts.append({"type": "input_text", "text": str(part.get("text", ""))})
            elif ptype == "image_url":
                parts.append({"type": "input_image", "image_url": part.get("image_url")})
        return parts if parts else ""
    return str(content)


def _to_responses_input(messages: List[Dict]) -> Tuple[List[Dict], List[str]]:
    """Chat messages -> (responses input items, system instructions)."""
    instructions: List[str] = []
    items: List[Dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        tool_calls = m.get("tool_calls")

        if role == "system":
            text = _content_text(content)
            if text:
                instructions.append(text)
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id") or "",
                "output": _content_text(content),
            })
            continue
        if role == "assistant" and tool_calls:
            text = _content_text(content)
            if text:
                items.append({"role": "assistant", "content": text})
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "",
                })
            continue

        items.append({"role": role or "user", "content": _convert_content(content)})
    return items, instructions


def _to_responses_tools(tools: Optional[list]) -> List[Dict]:
    """Chat tool schema -> /v1/responses tool schema (flatten 'function')."""
    out: List[Dict] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name:
            continue
        entry = {
            "type": "function",
            "name": name,
            "description": fn.get("description"),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        if fn.get("strict") is not None:
            entry["strict"] = bool(fn["strict"])
        out.append(entry)
    return out


def _parse_responses_output(data: Dict) -> Tuple[str, List[Dict]]:
    """Responses 'output' -> (result_str, tool_calls).

    result_str is the chat-completions-shaped tool_calls JSON when the model called a
    tool, else the concatenated text.
    """
    tool_calls: List[Dict] = []
    texts: List[str] = []
    for item in (data.get("output") or []):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "",
                },
            })
        elif itype == "message":
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, str):
                        texts.append(part)
                    elif isinstance(part, dict) and part.get("type") == "output_text":
                        texts.append(str(part.get("text") or ""))
    if tool_calls:
        return json.dumps({"type": "tool_calls", "tool_calls": tool_calls}), tool_calls
    text = "".join(texts).strip()
    return text or "[AI returned empty response]", []


def _estimate_input_tokens(messages: List[Dict], tools: Optional[list]) -> int:
    """Rough prompt cost in tokens, counting tool-call JSON and the tool schema."""
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        total += len(_content_text(m.get("content")))
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"]))
    if tools:
        total += len(json.dumps(tools))
    return total // 4


async def chat_responses(
    api_url: str,
    timeout: int,
    model: str,
    messages: List[Dict],
    tools: Optional[list],
    reasoning_effort: Optional[str] = None,
    context_window: Optional[int] = None,
    headers: Optional[dict] = None,
    websocket: Any = None,
) -> Tuple[str, TokenStats]:
    """One non-streaming /v1/responses call -> (result_str, TokenStats).

    reasoning_effort: OpenAI's spelling ("low"/"medium"/"high") already mapped from the
    canonical level; None sends no reasoning key.
    """
    items, instructions = _to_responses_input(messages)
    tools_schema = _to_responses_tools(tools)
    window = int(context_window or CONTEXT_WINDOW_TOKENS)

    est = _estimate_input_tokens(messages, tools)
    room = max(0, window - est - 512)
    max_output_tokens = min(OPENAI_MAX_NEW_TOKENS, max(LLAMA_MIN_SAFE_MAX_TOKENS, room))

    payload: Dict = {
        "model": model,
        "input": items,
        "stream": False,
        "max_output_tokens": max_output_tokens,
    }
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    if tools_schema:
        payload["tools"] = tools_schema
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}

    try:
        logger.info(f"Calling OpenAI /v1/responses for {model} (tools)")
        client = await get_async_client()
        start = time.time()
        resp = await client.post(f"{api_url}/responses", json=payload,
                                 headers=headers, timeout=timeout)
        if resp.status_code == 401:
            raise RuntimeError(
                "OpenAI rejected our credentials (HTTP 401). Put the key in "
                "config/openai_servers.json ('api_key') or config.OPENAI_API_KEY."
            )
        if resp.status_code == 404:
            raise FileNotFoundError(f"Model '{model}' is not served by OpenAI")
        if resp.status_code != 200:
            err_text = (await resp.aread()).decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"OpenAI returned HTTP {resp.status_code}: {err_text[:300]}")
        data = resp.json()
        duration = time.time() - start
    except httpx.ConnectError as e:
        raise ConnectionError("OpenAI is not reachable.") from e
    except httpx.TimeoutException as e:
        raise TimeoutError(f"OpenAI request timed out after {timeout} seconds.") from e
    except httpx.HTTPError as e:
        logger.warning(f"[OPENAI] connection dropped for /v1/responses model '{model}' "
                       f"({type(e).__name__}: {e})")
        raise ConnectionError("OpenAI connection dropped mid-response. Please try again.") from None

    result_str, tool_calls = _parse_responses_output(data)
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    generated_tokens = int(usage.get("output_tokens") or 0)
    if not generated_tokens and not tool_calls:
        generated_tokens = max(1, len(result_str) // 4)

    # One content frame before the final response, matching the chat path's streamed
    # content frames. Tool calls are returned atomically (as the chat path does too).
    if not tool_calls and result_str and websocket:
        try:
            await websocket.send_text(json.dumps({"type": "content", "content": result_str}))
        except Exception as e:  # noqa: BLE001 - adapter swallows/never raises
            logger.warning(f"Failed to send responses content frame: {e}")

    stats = TokenStats(
        prompt_tokens=prompt_tokens,
        generated_tokens=generated_tokens,
        generation_speed=round(generated_tokens / duration, 1) if duration > 0 else None,
        total_duration=round(duration, 2),
        provider_name="openai",
        model=model,
        context_window=window,
    )
    return result_str, stats
