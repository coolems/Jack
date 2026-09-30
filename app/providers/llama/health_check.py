"""Health check and model verification for llama.cpp server.

Provides functions to wait for the server to become healthy via HTTP /health
endpoint, verify the correct model is loaded, and query the current model name.
"""

import os
import time as _time
import logging
import httpx
import asyncio
from typing import Optional

logger = logging.getLogger("COOLEMS.Provider.Llama.Server")

from config import HEALTH_CHECK_TIMEOUT
from .http_client import get_async_client


async def _wait_for_health(api_url: str, max_attempts: int, sleep_sec: int = None) -> bool:
    """Wait for llama.cpp to respond to /health. Returns True if healthy.

    (2026-09 threadless refactor): async - polls on the shared httpx pool with
    await asyncio.sleep() between attempts instead of blocking time.sleep()."""
    from config import LLAMA_SERVER_STARTUP_WAIT_SEC

    if sleep_sec is None:
        sleep_sec = LLAMA_SERVER_STARTUP_WAIT_SEC

    client = await get_async_client()
    for _ in range(max_attempts):
        try:
            resp = await client.get(f"{api_url}/health")
            if resp.status_code == 200:
                return True
        except Exception:
            logger.debug("Health check: request failed for llama endpoint")
        await asyncio.sleep(sleep_sec)

    return False

async def _verify_model_loaded(api_url: str, expected_filename: str) -> bool:
    """Check if the expected model is actually loaded in the server.

    (2026-08-23 security fix) EXACT match only: strip .gguf from both sides,
    compare basenames case-insensitively for equality. The old bidirectional
    substring fallback could let a wrong/shorter id pass verification.

    (2026-09 threadless refactor): async - /v1/models via the shared httpx pool."""
    try:
        client = await get_async_client()
        models_resp = await client.get(f"{api_url}/v1/models")
        models_data = models_resp.json().get("data", [])
        if not models_data:
            return False

        loaded_model_id = models_data[0].get("id", "")

        # Strip .gguf specifically (not splitext which treats ".6" as extension)
        _req = os.path.basename(expected_filename).lower()
        _loaded = os.path.basename(loaded_model_id).lower()
        if _req.endswith('.gguf'):
            _req = _req[:-5]
        if _loaded.endswith('.gguf'):
            _loaded = _loaded[:-5]

        # EXACT equality only - no substring fallback (2026-08-23 security fix)
        if _req == _loaded:
            logger.info(f"[VERIFY] Model match (exact): '{_req}' == '{_loaded}'")
            return True

        logger.warning(f"[VERIFY] Model mismatch: expected '{_req}', got '{_loaded}'")
        return False

    except Exception as e:
        logger.warning(f"[VERIFY] Could not verify model: {e}")
        return False

async def get_current_model(api_url: str, force: bool = False) -> Optional[str]:
    """Get the currently loaded model name from the server.

    2026-08-18 OPTIMIZED — cache-first, pooled HTTP:
      1) Fresh in-memory state (model_state, written by server.py after every
         verified launch) is returned with ZERO HTTP calls. This is what the WS
         'current_model' handlers and UI polling hit — they used to pay a full
         /v1/models round-trip per call.
      2) Stale/empty cache -> ONE pooled /v1/models fetch (async httpx), then the
         answer is cached for subsequent calls within the TTL.
      3) API unreachable -> fall back to the tracked state (we know what we launched).

    force=True skips the fresh-cache shortcut and always verifies via /v1/models
    first (used right after a model switch so confirmation is immediate, not
    limited by LLAMA_MODEL_STATE_TTL_SEC)."""
    from config import LLAMA_MODEL_STATE_TTL_SEC
    from . import model_state

    # ---- 1) Fresh cache: no HTTP at all (skipped when force=True) ----
    # (2026-09-08 multi-chat) keyed per instance so each llama server's state is isolated.
    _key = model_state.key_for_api_url(api_url)
    if not force:
        cached, fresh = model_state.get_cached_model(LLAMA_MODEL_STATE_TTL_SEC, key=_key)
        if fresh:
            logger.debug(f"[MODEL] Cache hit (fresh): {cached}")
            return cached

    # ---- 2) Verify via API once (async pooled client - no loop freeze, no new TCP handshake) ----
    try:
        client = await get_async_client()
        resp = await client.get(f"{api_url}/v1/models")
        models_data = resp.json().get("data", [])
        if models_data:
            model_name = models_data[0].get("id", "")
            logger.info(f"[MODEL] API reports current model: {model_name}")
            if model_name:
                model_state.mark_loaded(model_name, key=_key)  # keep cache warm (per instance)
                return model_name
    except Exception:
        logger.debug("[MODEL] Failed to fetch current model from API")

    # ---- 3) Fallback: tracked state (prevents stale/None during reload) ----
    cached, _fresh_fallback = model_state.get_cached_model(LLAMA_MODEL_STATE_TTL_SEC, key=_key)
    if cached is not None:
        logger.info(f"[MODEL] Using tracked-state fallback: {cached}")
        return cached

    return None