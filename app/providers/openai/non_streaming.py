"""Non-streaming chat for the OpenAI hosted API, over the shared async HTTP pool."""

import logging
from typing import Dict, List, Optional

import httpx

from config import CONTEXT_WINDOW_TOKENS, OPENAI_MAX_NEW_TOKENS

from ..llama.converter import convert_messages_for_vision
from ..llama.http_client import get_async_client
from ..llama.thinking import normalize_mode
from .payload import build_openai_payload
from .thinking import thinking_body

logger = logging.getLogger("COOLEMS.Provider.OpenAI.NonStreaming")


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
    """One non-streaming OpenAI completion.

    thinking_mode: canonical level ("none"/"low"/"medium"/"high") translated into
    OpenAI's top-level reasoning_effort; None sends no reasoning keys.
    """
    mode = normalize_mode(thinking_mode) if thinking_mode else ""
    from .streaming import _policy_for
    body = thinking_body(_policy_for(api_url, model), mode)

    window = int(context_window or CONTEXT_WINDOW_TOKENS)
    messages = convert_messages_for_vision(messages)
    from .streaming import _estimate_prompt_tokens
    est = _estimate_prompt_tokens(messages, None)
    room = max(0, window - est - 512)
    max_tokens = min(OPENAI_MAX_NEW_TOKENS, max(1024, room))

    payload = build_openai_payload(
        model=model, messages=messages, stream=False, temperature=temperature,
        max_tokens=max_tokens, thinking_body=body,
    )

    try:
        logger.info(f"Calling OpenAI model (non-streaming): {model}")
        client = await get_async_client()
        resp = await client.post(f"{api_url}/chat/completions", json=payload,
                                 headers=headers, timeout=timeout)

        if resp.status_code == 401:
            raise RuntimeError(
                "OpenAI rejected our credentials (HTTP 401). Put the key in "
                "config/openai_servers.json ('api_key') or config.OPENAI_API_KEY."
            )
        if resp.status_code == 404:
            raise FileNotFoundError(f"Model '{model}' is not served by OpenAI")
        if resp.status_code != 200:
            err_text = (await resp.aread()).decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI returned HTTP {resp.status_code}: {err_text.strip()[:300]}")

        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message", {})
        content = (message.get("content") or "").strip()
        if not content:
            if data.get("error"):
                raise RuntimeError(f"OpenAI error: {data['error']}")
            content = "[AI returned empty response]"
        logger.info(f"OpenAI response: {len(content)} chars")
        return content

    except httpx.ConnectError as e:
        raise ConnectionError("OpenAI is not reachable.") from e
    except httpx.TimeoutException as e:
        raise TimeoutError(f"OpenAI request timed out after {timeout} seconds.") from e
    except httpx.HTTPError as e:
        logger.warning(f"[OPENAI] connection dropped for model '{model}' "
                       f"({type(e).__name__}: {e})")
        raise ConnectionError("OpenAI connection dropped mid-response. Please try again.") from None
