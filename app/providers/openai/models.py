"""OpenAI model listing - GET /models is the source of truth for the model ids.

Unlike DeepSeek, OpenAI's /models reports ONLY the id (plus created/owned metadata),
so context window, vision and reasoning effort come from a small static table keyed by
model family (longest prefix wins: gpt-4o-2024-11-20 -> gpt-4o). An id with no table
entry gets safe defaults: no vision, no reasoning effort, and the config context
fallback - the model still routes, it just cannot be told to think.
"""

import logging
from typing import Dict, List, Optional

from ..llama.http_client import get_async_client
from ..llama.model_identity import model_key as model_key_of
from .thinking import OpenAIThinkingPolicy

logger = logging.getLogger("COOLEMS.Provider.OpenAI.Models")

# Static per-family metadata: ctx (context window), max_output (output ceiling),
# vision (image input), efforts (reasoning_effort levels, cheapest first),
# default (the model's own default effort), placement (where reasoning_effort goes:
# "top" for o-series, "text" for gpt-5's text.format).
KNOWN_MODELS: Dict[str, dict] = {
    "gpt-4o": {"ctx": 128000, "max_output": 16384, "vision": True, "efforts": []},
    "gpt-4o-mini": {"ctx": 128000, "max_output": 16384, "vision": True, "efforts": []},
    "gpt-4o-audio": {"ctx": 128000, "max_output": 16384, "vision": True, "efforts": []},
    "gpt-4.1": {"ctx": 1000000, "max_output": 32768, "vision": True, "efforts": []},
    "gpt-5": {"ctx": 272000, "max_output": 128000, "vision": True,
              "efforts": ["low", "medium", "high"], "default": "medium", "placement": "text"},
    "gpt-5.1": {"ctx": 272000, "max_output": 128000, "vision": True,
                "efforts": ["low", "medium", "high"], "default": "medium", "placement": "text"},
    "gpt-5.2": {"ctx": 272000, "max_output": 128000, "vision": True,
                "efforts": ["low", "medium", "high"], "default": "medium", "placement": "text"},
    "gpt-5.4": {"ctx": 272000, "max_output": 128000, "vision": True,
                "efforts": ["low", "medium", "high"], "default": "medium", "placement": "text"},
    "gpt-5.5": {"ctx": 272000, "max_output": 128000, "vision": True,
                "efforts": ["low", "medium", "high"], "default": "medium", "placement": "text"},
    "gpt-5.6": {"ctx": 272000, "max_output": 128000, "vision": True,
                "efforts": ["low", "medium", "high"], "default": "medium", "placement": "text"},
    "gpt-6": {"ctx": 272000, "max_output": 128000, "vision": True,
              "efforts": ["low", "medium", "high"], "default": "medium", "placement": "text"},
    "o1": {"ctx": 200000, "max_output": 100000, "vision": True,
           "efforts": ["low", "medium", "high"], "default": "medium", "placement": "top"},
    "o3": {"ctx": 200000, "max_output": 100000, "vision": True,
           "efforts": ["low", "medium", "high"], "default": "medium", "placement": "top"},
    "o4-mini": {"ctx": 200000, "max_output": 100000, "vision": True,
                "efforts": ["low", "medium", "high"], "default": "medium", "placement": "top"},
}


def metadata_for(model_id: str) -> dict:
    """The static metadata for one model id: exact table row first, then the longest
    matching family prefix, then safe defaults (no vision, no efforts, no ctx).

    A family matches when the id starts with it AND the next char is not a digit, so
    "gpt-5.1-2025-11-13" matches gpt-5.1 (not gpt-5) and "gpt-5-2025-08-07" matches gpt-5.
    """
    mid = (model_id or "").strip().lower()
    if mid in KNOWN_MODELS:
        return dict(KNOWN_MODELS[mid])
    best = ""
    for family in KNOWN_MODELS:
        if not mid.startswith(family):
            continue
        nxt = mid[len(family):len(family) + 1]
        if nxt and nxt.isdigit():
            continue
        if len(family) > len(best):
            best = family
    if best:
        return dict(KNOWN_MODELS[best])
    return {"ctx": None, "max_output": None, "vision": False, "efforts": []}


def parse_models(payload: dict, override: Optional[dict] = None) -> List[Dict]:
    """Turn a /models response into our catalog-shaped entries.

    Each entry: id (routing key), name (display), ctx, max_output, vision, thinking
    (OpenAIThinkingPolicy built from the static table), on_disk False. Malformed
    entries are skipped, never fatal.
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
        meta = metadata_for(mid)
        out.append({
            "id": mid,
            "key": key,
            "name": str(m.get("name") or mid),
            "ctx": meta.get("ctx"),
            "max_output": meta.get("max_output"),
            "vision": bool(meta.get("vision")),
            "thinking": OpenAIThinkingPolicy.from_model(meta, override),
            "raw": m,
        })
    return out


async def fetch_models(api_url: str, headers: Optional[dict] = None) -> List[Dict]:
    """GET /models and parse it. Raises on a non-200 so health can report the reason."""
    client = await get_async_client()
    resp = await client.get(f"{api_url}/models", timeout=10.0, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI /models returned HTTP {resp.status_code}")
    models = parse_models(resp.json())
    logger.info("[OPENAI] Serving %d model(s): %s", len(models),
                ", ".join(m["id"] for m in models) or "(none)")
    return models
