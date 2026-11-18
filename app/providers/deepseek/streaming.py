"""Streaming chat for the DeepSeek hosted API.

Same SSE loop shape as llama/streaming.py (content + reasoning buffering, tool-call
delta accumulation, stop_event, stall watchdog, real token stats from the final chunk),
with DeepSeek's request body (deepseek/payload.py) and its /models metadata for the
context window. delta.reasoning_content -> {type:"thinking"} frames, exactly as llama's
path already does, so the CLIENT sees the same frame types.

DeepSeek is attach-only: a failed pre-flight marks the backend unhealthy and never
starts a process.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import (
    CONTEXT_WINDOW_TOKENS,
    DEEPSEEK_MAX_NEW_TOKENS,
    LLAMA_MIN_SAFE_MAX_TOKENS,
    LLAMA_STREAM_STALL_TIMEOUT_SEC,
    STREAM_BUFFER_SIZE_CHARS,
    STREAM_BUFFER_SIZE_MSGS,
    STREAM_BUFFER_TIMEOUT_SEC,
)

from ..llama.converter import convert_messages_for_vision
from ..llama.http_client import get_async_client
from ..llama.streaming import _iter_sse_with_stall
from ..llama.thinking import normalize_mode
from ..token_stats import TokenStats
from .payload import build_deepseek_payload
from .thinking import DEEPSEEK_EFFORTS, DeepSeekThinkingPolicy, thinking_body

logger = logging.getLogger("COOLEMS.Provider.DeepSeek.Streaming")

# Flat estimate per attached image (DeepSeek's vision encoder, like llama's guard).
_VISION_TOKENS_PER_IMAGE = 1500

# Rough share of the output room reasoning is likely to take, per canonical level.
_THINK_RESERVE_TOKENS = {"low": 512, "medium": 1536, "high": 4096}


def _policy_for(api_url: str, model: str) -> DeepSeekThinkingPolicy:
    """Thinking policy of the DeepSeek model that will answer, from the registry.

    No registry (direct use, tests) -> a policy with the documented levels, so a plain
    call still gets valid reasoning keys.
    """
    try:
        from ..llama.backend_registry import get_registry
        reg = get_registry()
        if reg is not None:
            # The per-model CHILD backend holds this model's policy; the endpoint-level
            # backend has none discovered, so ask by model explicitly.
            backend = reg.get_for_api_url(api_url, model or "")
            if backend is not None and isinstance(backend.thinking, DeepSeekThinkingPolicy):
                return backend.thinking
            policy = reg.thinking_for_model(model or "")
            if isinstance(policy, DeepSeekThinkingPolicy):
                return policy
    except Exception:  # pragma: no cover - defensive
        pass
    return DeepSeekThinkingPolicy.from_model(
        {"effort": {"supported_levels": list(DEEPSEEK_EFFORTS), "default_level": "high"}})


def _msg_chars(m: dict) -> int:
    """Prompt cost of one message: text at chars/4, images at a flat estimate."""
    content = m.get("content", "")
    if isinstance(content, list):
        chars = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                chars += len(str(part.get("text", "")))
            elif part.get("type") == "image_url":
                chars += _VISION_TOKENS_PER_IMAGE * 4
    else:
        chars = len(str(content))
    tcs = m.get("tool_calls")
    if tcs:
        try:
            chars += len(json.dumps(tcs))
        except (TypeError, ValueError):
            chars += len(str(tcs))
    rc = m.get("reasoning_content")
    if rc:
        chars += len(str(rc))
    return chars


async def chat_stream(
    api_url: str,
    timeout: int,
    model: str,
    messages: List[Dict],
    websocket: Any,
    temperature: float = 0.7,
    enable_thinking: Any = False,
    conv_id: Optional[str] = None,
    tools: Optional[list] = None,
    stop_event: Optional[asyncio.Event] = None,
    context_window: Optional[int] = None,
    headers: Optional[dict] = None,
    thinking_mode: Optional[str] = None,
) -> Tuple[str, TokenStats]:
    """Stream a DeepSeek completion via SSE -> (response_str, TokenStats).

    thinking_mode / enable_thinking carry the canonical level and the display decision,
    same contract as llama's chat_stream: a level is translated into DeepSeek's
    thinking.type + reasoning_effort, a level other than 'none' shows reasoning.
    """
    window = int(context_window or CONTEXT_WINDOW_TOKENS)

    # DeepSeek speaks OpenAI multimodal: the CLIENT attaches images as the Ollama-style
    # 'images' key (base64 blobs), which DeepSeek does not understand. Convert it to
    # image_url content exactly as the llama path does before the body is built.
    messages = convert_messages_for_vision(messages)

    mode = normalize_mode(thinking_mode) if thinking_mode not in (None, "") else ""
    if not mode and isinstance(enable_thinking, str):
        mode = normalize_mode(enable_thinking)
    policy = _policy_for(api_url, model)
    body = thinking_body(policy, mode)
    display = (mode != "none") if mode else bool(enable_thinking)

    payload = build_deepseek_payload(
        model=model,
        messages=messages,
        stream=True,
        temperature=temperature,
        tools=tools,
        top_p=None,
        max_tokens=_safe_max_tokens(messages, tools, window, mode, display),
        thinking_body=body,
    )

    # ---- pre-flight: the endpoint must be routable ----
    # The registry owns DeepSeek health (its poller probes GET /models), so a request that
    # landed on a healthy backend needs no extra round-trip. Only a direct call with no
    # registry (tests, CLI use) probes /models itself.
    from ..llama.backend_registry import get_registry
    reg = get_registry()
    backend = reg.get_for_api_url(api_url, model or "") if reg is not None else None
    if backend is not None:
        if not backend.healthy:
            raise ConnectionError(
                f"DeepSeek is not reachable at {api_url}. Check the Bearer key in "
                "config/deepseek_servers.json ('api_key') or config.DEEPSEEK_API_KEY."
            )
    else:
        try:
            client = await get_async_client()
            resp = await client.get(f"{api_url}/models", timeout=10.0, headers=headers)
            if resp.status_code != 200:
                raise ConnectionError(f"DeepSeek health check returned HTTP {resp.status_code}")
        except Exception:
            raise ConnectionError(
                f"DeepSeek is not reachable at {api_url}. Check the Bearer key in "
                "config/deepseek_servers.json ('api_key') or config.DEEPSEEK_API_KEY."
            )

    full_response_parts: List[str] = []
    content_buffer = ""
    think_buffer = ""
    message_counter = 0
    last_send_time = asyncio.get_running_loop().time()
    accumulated_tool_calls: List[dict] = []
    prompt_tokens = 0
    completion_tokens = 0
    finish_reason: Optional[str] = None
    was_stopped = False

    async def flush_content():
        nonlocal content_buffer, message_counter, last_send_time
        if content_buffer and websocket:
            try:
                await websocket.send_text(json.dumps({"type": "content", "content": content_buffer}))
                content_buffer = ""
                message_counter = 0
                last_send_time = asyncio.get_running_loop().time()
            except Exception as e:
                logger.warning(f"Failed to send content buffer: {e}")
                raise

    async def flush_thinking():
        nonlocal think_buffer
        if think_buffer and websocket:
            try:
                await websocket.send_text(json.dumps({"type": "thinking", "content": think_buffer}))
                think_buffer = ""
            except Exception as e:
                logger.warning(f"Failed to send thinking buffer: {e}")
                raise

    est_prompt_tokens = _estimate_prompt_tokens(messages, tools)

    try:
        logger.info(f"Calling DeepSeek model with STREAMING: {model}")
        start_time = time.time()
        http_client = await get_async_client()

        async with http_client.stream(
            "POST",
            f"{api_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=10.0, read=timeout, write=timeout, pool=10.0),
        ) as response:
            if response.status_code == 401:
                raise RuntimeError(
                    f"DeepSeek rejected our credentials (HTTP 401). Put the key in "
                    "config/deepseek_servers.json ('api_key') or config.DEEPSEEK_API_KEY "
                    "(env DEEPSEEK_API_KEY also works)."
                )
            if response.status_code == 404:
                raise FileNotFoundError(f"Model '{model}' is not served by DeepSeek")
            if response.status_code != 200:
                body_text = (await response.aread()).decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"DeepSeek returned HTTP {response.status_code}: {body_text[:300]}")

            effective_stall = max(LLAMA_STREAM_STALL_TIMEOUT_SEC, min(600, est_prompt_tokens // 400))
            async for line in _iter_sse_with_stall(response.aiter_lines(), effective_stall,
                                                   conv_id or "", api_url):
                if stop_event and stop_event.is_set():
                    was_stopped = True
                    logger.info(f"Stop requested for conversation {conv_id}, ending generation")
                    await flush_content()
                    await flush_thinking()
                    break
                if not line.strip() or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    usage = data.get("usage") or {}
                    if usage:
                        prompt_tokens = usage.get("prompt_tokens", 0) or prompt_tokens
                        completion_tokens = usage.get("completion_tokens", 0) or completion_tokens
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0]["finish_reason"]
                    delta = choices[0].get("delta") or {}

                    if delta.get("tool_calls"):
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta.get("index", 0)
                            while len(accumulated_tool_calls) <= idx:
                                accumulated_tool_calls.append(
                                    {"id": "", "type": "function",
                                     "function": {"name": "", "arguments": ""}})
                            acc = accumulated_tool_calls[idx]
                            if tc_delta.get("id"):
                                acc["id"] = tc_delta["id"]
                            if tc_delta.get("type"):
                                acc["type"] = tc_delta["type"]
                            func = tc_delta.get("function") or {}
                            if func.get("name"):
                                acc["function"]["name"] = func["name"]
                            if func.get("arguments"):
                                acc["function"]["arguments"] += func["arguments"]

                    if display:
                        think_chunk = delta.get("reasoning_content") or ""
                        if think_chunk:
                            think_buffer += think_chunk
                            await flush_thinking()

                    chunk = delta.get("content") or ""
                    if chunk:
                        full_response_parts.append(chunk)
                        content_buffer += chunk
                        message_counter += 1
                        now = asyncio.get_running_loop().time()
                        if (len(content_buffer) >= STREAM_BUFFER_SIZE_CHARS
                                or message_counter >= STREAM_BUFFER_SIZE_MSGS
                                or (now - last_send_time) >= STREAM_BUFFER_TIMEOUT_SEC):
                            await flush_content()
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse SSE line: {line[:100]}")
                    continue

            if not was_stopped:
                await flush_content()
                await flush_thinking()

        total_duration = time.time() - start_time
        full_response = "".join(full_response_parts)
        if not prompt_tokens and not completion_tokens:
            logger.warning(f"No token statistics received from DeepSeek! Response length: "
                           f"{len(full_response)} chars")
            prompt_tokens = max(1, est_prompt_tokens)
            completion_tokens = max(1, len(full_response) // 4)

        was_truncated = bool(finish_reason == "length" and not accumulated_tool_calls)
        if finish_reason:
            logger.info(f"[DeepSeek] finish_reason={finish_reason!r} (truncated={was_truncated}, "
                        f"completion_tokens={completion_tokens})")
        if was_truncated:
            logger.warning(f"[TRUNCATED] Generation hit the output limit "
                           f"({payload['max_tokens']} tokens) before finishing.")

        stats = TokenStats(
            prompt_tokens=prompt_tokens,
            generated_tokens=completion_tokens,
            truncated=was_truncated,
            generation_speed=round(completion_tokens / total_duration, 1)
            if total_duration > 0 and completion_tokens > 0 else None,
            total_duration=round(total_duration, 2),
            provider_name="deepseek",
            model=model,
            context_window=window,
        )

        if accumulated_tool_calls:
            logger.info(f"Received {len(accumulated_tool_calls)} native tool call(s)")
            return json.dumps({"type": "tool_calls", "tool_calls": accumulated_tool_calls}), stats
        return full_response, stats

    except httpx.ConnectError as e:
        logger.error(f"Cannot connect to DeepSeek: {e}")
        raise ConnectionError("DeepSeek is not reachable. Check the endpoint and network.")
    except httpx.TimeoutException as e:
        logger.error(f"DeepSeek timeout after {timeout}s: {e}")
        raise TimeoutError(f"DeepSeek request timed out after {timeout} seconds.")
    except httpx.HTTPError as e:
        logger.warning(f"[DEEPSEEK] connection dropped mid-stream for model '{model}' "
                       f"({type(e).__name__}: {e})")
        raise ConnectionError("DeepSeek connection dropped mid-response. Please try again.") from None


def _estimate_prompt_tokens(messages: List[Dict], tools: Optional[list]) -> int:
    """Rough prompt cost, counting tool-call JSON and images (see _msg_chars)."""
    total = sum(_msg_chars(m) for m in messages if isinstance(m, dict)) // 4
    if tools:
        try:
            total += len(json.dumps(tools)) // 4
        except Exception:
            pass
    return total


def _safe_max_tokens(messages: List[Dict], tools: Optional[list], window: int,
                     mode: str, display: bool) -> int:
    """Clamp max_tokens to the room the model's context window actually leaves."""
    est = _estimate_prompt_tokens(messages, tools)
    reserve = _THINK_RESERVE_TOKENS.get(mode, 0) if display else 0
    room = max(0, window - est - 512)
    safe = min(DEEPSEEK_MAX_NEW_TOKENS, max(LLAMA_MIN_SAFE_MAX_TOKENS, room))
    if safe < DEEPSEEK_MAX_NEW_TOKENS:
        logger.warning(f"[CONTEXT-GUARD] Prompt ~{est} tokens leaves only "
                       f"{max(0, window - est)} free of {window}. Clamping max_tokens to {safe}.")
    if safe <= LLAMA_MIN_SAFE_MAX_TOKENS:
        logger.warning(f"[CONTEXT-GUARD] *** OUTPUT ROOM AT FLOOR ({safe} tokens) *** "
                       f"prompt ~{est}/{window}. Long answers will be truncated.")
    if reserve and room < LLAMA_MIN_SAFE_MAX_TOKENS + reserve:
        logger.warning(f"[CONTEXT-GUARD] Thinking is on ({mode}, ~{reserve} reasoning tokens) "
                       f"but only {room} tokens of output room remain.")
    return safe
