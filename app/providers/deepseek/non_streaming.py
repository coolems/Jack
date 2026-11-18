"""Non-streaming chat for the DeepSeek hosted API, over the shared async HTTP pool."""

import logging
from typing import Dict, List, Optional

import httpx

from config import CONTEXT_WINDOW_TOKENS, DEEPSEEK_MAX_NEW_TOKENS

from ..llama.converter import convert_messages_for_vision
from ..llama.http_client import get_async_client
from ..llama.thinking import normalize_mode
from .payload import build_deepseek_payload
from .thinking import thinking_body

logger = logging.getLogger("COOLEMS.Provider.DeepSeek.NonStreaming")


async def chat_non_streaming(
    api_url: str,
    timeout: int,
    model: str,
    messages: List[Dict],
    temperature: float = 0.7,
    headers: Optional[dict] = None,
    thinking_mode: Optional[str] = None,
    context_window: Optional[int] = None,
) -> str:
    """One non-streaming DeepSeek completion.

    thinking_mode: canonical level ("none"/"low"/"medium"/"high") translated into
    DeepSeek's thinking.type + reasoning_effort; None sends no reasoning keys.
    """
    mode = normalize_mode(thinking_mode) if thinking_mode else ""
    from .streaming import _policy_for
    body = thinking_body(_policy_for(api_url, model), mode)

    window = int(context_window or CONTEXT_WINDOW_TOKENS)
    messages = convert_messages_for_vision(messages)
    from .streaming import _estimate_prompt_tokens
    est = _estimate_prompt_tokens(messages, None)
    room = max(0, window - est - 512)
    max_tokens = min(DEEPSEEK_MAX_NEW_TOKENS, max(1024, room))

    payload = build_deepseek_payload(
        model=model, messages=messages, stream=False, temperature=temperature,
        max_tokens=max_tokens, thinking_body=body,
    )

    try:
        logger.info(f"Calling DeepSeek model (non-streaming): {model}")
        client = await get_async_client()
        resp = await client.post(f"{api_url}/chat/completions", json=payload,
                                 headers=headers, timeout=timeout)

        if resp.status_code == 401:
            raise RuntimeError(
                "DeepSeek rejected our credentials (HTTP 401). Put the key in "
                "config/deepseek_servers.json ('api_key') or config.DEEPSEEK_API_KEY."
            )
        if resp.status_code == 404:
            raise FileNotFoundError(f"Model '{model}' is not served by DeepSeek")
        if resp.status_code != 200:
            err_text = (await resp.aread()).decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek returned HTTP {resp.status_code}: {err_text.strip()[:300]}")

        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message", {})
        content = (message.get("content") or "").strip()
        if not content:
            if data.get("error"):
                raise RuntimeError(f"DeepSeek error: {data['error']}")
            content = "[AI returned empty response]"
        logger.info(f"DeepSeek response: {len(content)} chars")
        return content

    except httpx.ConnectError as e:
        raise ConnectionError("DeepSeek is not reachable.") from e
    except httpx.TimeoutException as e:
        raise TimeoutError(f"DeepSeek request timed out after {timeout} seconds.") from e
    except httpx.HTTPError as e:
        logger.warning(f"[DEEPSEEK] connection dropped for model '{model}' "
                       f"({type(e).__name__}: {e})")
        raise ConnectionError("DeepSeek connection dropped mid-response. Please try again.") from None
