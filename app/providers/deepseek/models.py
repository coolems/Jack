"""DeepSeek model listing - GET /models is the source of truth.

DeepSeek reports per-model metadata the registry needs: id (the routing key), display
name, context_window, max_output_tokens, input_modalities (vision) and the effort levels
the model accepts. Nothing is scanned from disk and nothing is guessed.
"""

import logging
from typing import Dict, List, Optional

from ..llama.http_client import get_async_client
from ..llama.model_identity import model_key as model_key_of
from .thinking import DeepSeekThinkingPolicy

logger = logging.getLogger("COOLEMS.Provider.DeepSeek.Models")

# How long a /models answer is trusted before re-fetching (the list rarely changes).
_MODELS_TTL_SEC = 300.0


def parse_models(payload: dict, override: Optional[dict] = None) -> List[Dict]:
    """Turn a /models response into our catalog-shaped entries.

    Each entry: id (routing key), name (display), ctx, max_output, vision, thinking
    (DeepSeekThinkingPolicy), on_disk False. Malformed entries are skipped, never fatal.
    """
    out: List[Dict] = []
    seen = set()
    for m in (payload or {}).get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        key = model_key_of(mid)
        if not key or key in seen:
            continue
        seen.add(key)
        ctx = m.get("context_window")
        out.append({
            "id": mid,
            "key": key,
            "name": str(m.get("name") or mid),
            "ctx": ctx if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0 else None,
            "max_output": (m.get("max_output_tokens")
                           if isinstance(m.get("max_output_tokens"), int) else None),
            "vision": "image" in (m.get("input_modalities") or []),
            "thinking": DeepSeekThinkingPolicy.from_model(m, override),
            "raw": m,
        })
    return out


async def fetch_models(api_url: str, headers: Optional[dict] = None) -> List[Dict]:
    """GET /models and parse it. Raises on a non-200 so health can report the reason."""
    client = await get_async_client()
    resp = await client.get(f"{api_url}/models", timeout=10.0, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"DeepSeek /models returned HTTP {resp.status_code}")
    models = parse_models(resp.json())
    logger.info("[DEEPSEEK] Serving %d model(s): %s", len(models),
                ", ".join(m["id"] for m in models) or "(none)")
    return models
