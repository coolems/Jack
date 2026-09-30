"""
Non-streaming chat for llama.cpp provider.

Uses shared async HTTP client pool from http_client module instead of sync
requests.post() — this unblocks the event loop and enables connection reuse.
"""
import logging
from typing import List, Dict

import httpx

from config import CONTEXT_WINDOW_TOKENS

from .converter import convert_messages_for_vision
from .vision_support import make_vision_error, _looks_like_mmproj_error
from .payload import build_llama_payload
from .http_client import get_async_client

logger = logging.getLogger("COOLEMS.Provider.Llama.NonStreaming")


async def chat_non_streaming(
    api_url: str,
    timeout: int,
    model: str,
    messages: List[Dict],
    temperature: float = 0.7,
) -> str:
    """Call llama.cpp model without streaming using shared async client pool."""
    messages = convert_messages_for_vision(messages)

    llama_payload = build_llama_payload(
        model=model,
        messages=messages,
        stream=False,
        temperature=temperature,
        max_tokens=CONTEXT_WINDOW_TOKENS,
    )

    try:
        logger.info(f"Calling llama.cpp model (non-streaming): {model}")

        # Use shared async client pool — no blocking sync requests
        http_client = await get_async_client()
        resp = await http_client.post(
            f"{api_url}/v1/chat/completions",
            json=llama_payload,
            timeout=timeout,
        )

        if resp.status_code == 404:
            raise FileNotFoundError(f"Model '{model}' not found in llama.cpp")
        elif resp.status_code != 200:
            # (2026-08-23) Read the body so a vision rejection (no mmproj loaded)
            # is reported with a clean, actionable message instead of a bare HTTP code.
            err_text = (await resp.aread()).decode("utf-8", errors="replace")
            if _looks_like_mmproj_error(err_text):
                logger.error(f"[VISION] llama.cpp rejected image input: no mmproj loaded for '{model}'. Body: {err_text[:300]}")
                raise make_vision_error(model) from None
            raise RuntimeError(f"llama.cpp returned HTTP {resp.status_code}: {err_text.strip()[:300]}")

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

        if not content:
            if "error" in data:
                raise RuntimeError(f"llama.cpp error: {data['error']}")
            content = "[AI returned empty response]"

        logger.info(f"llama.cpp response: {len(content)} chars")
        return content

    except httpx.ConnectError as e:
        raise ConnectionError("llama.cpp service is not running.") from e
    except httpx.TimeoutException as e:
        raise TimeoutError(f"llama.cpp request timed out after {timeout} seconds.") from e
