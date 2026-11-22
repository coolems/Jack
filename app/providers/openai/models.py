"""OpenAI model listing - GET /models is the source of truth for the model ids.

Unlike DeepSeek, OpenAI's /models reports ONLY the id (plus created/owned metadata),
so context window, vision and reasoning effort come from a small static table keyed by
model family (longest prefix wins: gpt-4o-2024-11-20 -> gpt-4o). An id with no table
entry gets safe defaults: no vision, no reasoning effort, and the config context
fallback - the model still routes, it just cannot be told to think.

The request SHAPE (reasoning levels, default effort, where the reasoning key goes)
comes from openai/wire_profiles.py, not from this table: ctx/max_output/vision are the
only fields declared here.
"""

import logging
from typing import Dict, List, Optional

from ..llama.http_client import get_async_client
from ..llama.model_identity import model_key as model_key_of
from .thinking import OpenAIThinkingPolicy
from .wire_profiles import family_for, profile_for

logger = logging.getLogger("COOLEMS.Provider.OpenAI.Models")

# Static per-family metadata: ctx (context window), max_output (output ceiling),
# vision (image input). Reasoning fields come from wire_profiles for the same family.
KNOWN_MODELS: Dict[str, dict] = {
    "gpt-4o": {"ctx": 128000, "max_output": 16384, "vision": True},
    "gpt-4o-mini": {"ctx": 128000, "max_output": 16384, "vision": True},
    "gpt-4o-audio": {"ctx": 128000, "max_output": 16384, "vision": True},
    "gpt-4.1": {"ctx": 1000000, "max_output": 32768, "vision": True},
    "gpt-5": {"ctx": 272000, "max_output": 128000, "vision": True},
    "gpt-5.1": {"ctx": 272000, "max_output": 128000, "vision": True},
    "gpt-5.2": {"ctx": 272000, "max_output": 128000, "vision": True},
    "gpt-5.4": {"ctx": 272000, "max_output": 128000, "vision": True},
    "gpt-5.5": {"ctx": 272000, "max_output": 128000, "vision": True},
    "gpt-5.6": {"ctx": 272000, "max_output": 128000, "vision": True},
    "gpt-6": {"ctx": 272000, "max_output": 128000, "vision": True},
    "o1": {"ctx": 200000, "max_output": 100000, "vision": True},
    "o3": {"ctx": 200000, "max_output": 100000, "vision": True},
    "o4-mini": {"ctx": 200000, "max_output": 100000, "vision": True},
}


def metadata_for(model_id: str) -> dict:
    """The static metadata for one model id: exact table row first, then the longest
    matching family prefix, then safe defaults (no vision, no efforts, no ctx).

    Family matching is wire_profiles.family_for (a family matches when the id starts
    with it AND the next char is not a digit, so "gpt-5.1-2025-11-13" matches gpt-5.1
    and "gpt-5-2025-08-07" matches gpt-5). The reasoning fields are merged in from the
    family's wire profile, so the shape lives in exactly one table.
    """
    family = family_for(model_id)
    meta = dict(KNOWN_MODELS.get(family) or {"ctx": None, "max_output": None,
                                             "vision": False})
    profile = profile_for(model_id)
    meta["efforts"] = list(profile.efforts)
    if profile.reasoning_key:
        meta["default"] = profile.default_effort
        meta["placement"] = profile.reasoning_placement
    return meta


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
