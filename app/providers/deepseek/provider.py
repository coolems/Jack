"""DeepSeekProvider - the SERVER talking to DeepSeek's hosted API.

config.SERVER_BACKEND picks the engine behind the relay brain: "llama", "strata" or
"deepseek". Everything around generation (WebSocket relay, chat queue, worker pool, auth
profiles, streaming frames, token stats, stop button) is shared; what differs is the wire
shape and the lifecycle:

  request   our llama-shaped values -> DeepSeek body (payload.py): no top_k/min_p/
            presence_penalty/chat_template_kwargs, thinking{type} + reasoning_effort
            instead, stream_options only with stream=true
  response  SSE delta.content / delta.reasoning_content / tool-call deltas / usage ->
            our content + {type:"thinking"} frames + TokenStats (streaming.py)
  models    GET /models is the source of truth (id, context_window, max_output_tokens,
            input_modalities, effort levels) - no disk scan, no /props
  lifecycle attach-only: no process, no model loading; every request carries a Bearer key
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from config import (
    DEEPSEEK_BASE_URL,
    HEALTH_CHECK_INTERVAL_SEC,
    PROVIDER_DEFAULT_TIMEOUT,
    deepseek_auth_headers,
)

from ..base import BaseProvider
from ..llama.http_client import close_async_client
from ..llama.model_identity import model_key as model_key_of
from ..llama.vision_support import VisionNotAvailableError
from ..token_stats import TokenStats
from .models import fetch_models
from .non_streaming import chat_non_streaming as _chat_non_streaming
from .payload import transform_llama_payload
from .streaming import chat_stream as _chat_stream
from .thinking import DeepSeekThinkingPolicy, thinking_body

logger = logging.getLogger("COOLEMS.Provider.DeepSeek")


class DeepSeekProvider(BaseProvider):
    """DeepSeek hosted-API provider (OpenAI-compatible body, Bearer auth, attach-only)."""

    def __init__(self, api_url: str = DEEPSEEK_BASE_URL,
                 timeout: int = PROVIDER_DEFAULT_TIMEOUT,
                 headers: Optional[dict] = None):
        """Args:
            api_url: DeepSeek base URL (config.DEEPSEEK_BASE_URL or a deepseek_servers.json entry).
            timeout: Generation budget in seconds (same budget as the llama path).
            headers: Auth headers; None falls back to the matching deepseek_servers.json entry,
                     then config.DEEPSEEK_API_KEY, then env DEEPSEEK_API_KEY.
        """
        self._api_url = (api_url or DEEPSEEK_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._headers = headers if headers is not None else deepseek_headers_for(self._api_url)
        self._is_healthy = True
        self._last_check = 0.0
        self._models: List[Dict] = []

    @property
    def name(self) -> str:
        return "deepseek"

    @property
    def api_url(self) -> str:
        return self._api_url

    @property
    def headers(self) -> Optional[dict]:
        """The Bearer headers this endpoint is called with (None = no key configured)."""
        return self._headers

    # ---- model metadata ---------------------------------------------------

    async def _models_cached(self, force: bool = False) -> List[Dict]:
        """Parsed /models entries, refreshed at most once per HEALTH_CHECK_INTERVAL_SEC."""
        import time
        if self._models and not force and (time.time() - self._last_check) < HEALTH_CHECK_INTERVAL_SEC:
            return self._models
        try:
            self._models = await fetch_models(self._api_url, self._headers)
        except Exception as e:
            logger.warning("[DEEPSEEK] Could not list models from %s: %s", self._api_url, e)
            return self._models
        return self._models

    def _entry_for(self, model: str) -> Optional[Dict]:
        """The /models entry a request names, by canonical key."""
        want = model_key_of(model or "")
        for m in self._models:
            if m["key"] == want:
                return m
        return None

    # ---- chat_stream ------------------------------------------------------

    async def chat_stream(
        self,
        model: str,
        messages: List[Dict],
        websocket: Any,
        temperature: float = 0.7,
        enable_thinking=None,
        conv_id: str = None,
        tools: list = None,
        stop_event: Optional[asyncio.Event] = None,
        context_window: Optional[int] = None,
    ) -> Tuple[str, TokenStats]:
        """Stream a DeepSeek completion through the shared SSE machinery.

        enable_thinking: bool (show reasoning) or a canonical level "none"/"low"/"medium"/
        "high"; a level is translated into DeepSeek's thinking.type + reasoning_effort.
        """
        if self._has_image_content(messages) and not self._model_supports_images(model):
            raise self._vision_error(model)
        window = context_window or self._model_context(model)
        try:
            return await _chat_stream(
                api_url=self._api_url, timeout=self._timeout, model=model,
                messages=messages, websocket=websocket, temperature=temperature,
                enable_thinking=enable_thinking, conv_id=conv_id, tools=tools,
                stop_event=stop_event, context_window=window, headers=self._headers,
                thinking_mode=enable_thinking if isinstance(enable_thinking, str) else None,
            )
        except Exception as exc:  # noqa: BLE001 - re-worded below, never swallowed
            raise self._deepseek_error(exc) from exc

    # ---- chat_non_streaming -----------------------------------------------

    async def chat_non_streaming(self, model: str, messages: List[Dict],
                                 temperature: float = 0.7, enable_thinking=None) -> str:
        """Non-streaming completion against DeepSeek (same body transform)."""
        if self._has_image_content(messages) and not self._model_supports_images(model):
            raise self._vision_error(model)
        try:
            return await _chat_non_streaming(
                api_url=self._api_url, timeout=self._timeout, model=model,
                messages=messages, temperature=temperature, headers=self._headers,
                thinking_mode=enable_thinking if isinstance(enable_thinking, str) else None,
                context_window=self._model_context(model),
            )
        except Exception as exc:  # noqa: BLE001 - re-worded below, never swallowed
            raise self._deepseek_error(exc) from exc

    # ---- build_payload ----------------------------------------------------

    def build_payload(self, model: str, messages: List[Dict], *, stream: bool = True,
                      temperature: float = 0.7, enable_thinking=None,
                      tools: list = None, thinking_kwargs: Dict = None) -> Dict:
        """The DeepSeek body for this request (llama keys dropped, reasoning keys added).

        thinking_kwargs is accepted for signature compatibility and ignored: DeepSeek has
        no chat_template_kwargs, its reasoning keys come from the model's effort levels.
        """
        from ..llama.payload import build_llama_payload
        mode = enable_thinking if isinstance(enable_thinking, str) else None
        body = transform_llama_payload(
            build_llama_payload(model, messages, stream=stream, temperature=temperature,
                                tools=tools),
            thinking_body=thinking_body(self._policy_for(model), mode),
        )
        return body

    # ---- capability -------------------------------------------------------

    def _policy_for(self, model: str) -> DeepSeekThinkingPolicy:
        """Thinking policy of this model from /models (registry first, then our own list)."""
        try:
            from ..llama.backend_registry import get_registry
            reg = get_registry()
            if reg is not None:
                policy = reg.thinking_for_model(model or "")
                if isinstance(policy, DeepSeekThinkingPolicy):
                    return policy
        except Exception:  # pragma: no cover - defensive
            pass
        entry = self._entry_for(model)
        if entry is not None:
            return entry["thinking"]
        return DeepSeekThinkingPolicy.from_model({})

    def _model_context(self, model: str) -> int:
        """Context window of this model from /models; registry value wins, else config fallback."""
        try:
            from ..llama.backend_registry import get_registry
            reg = get_registry()
            if reg is not None:
                for b in reg.backends_for_model(model or ""):
                    if b.ctx_per_slot:
                        return int(b.ctx_per_slot)
        except Exception:  # pragma: no cover - defensive
            pass
        entry = self._entry_for(model)
        if entry and entry.get("ctx"):
            return int(entry["ctx"])
        from config import CONTEXT_WINDOW_TOKENS
        return CONTEXT_WINDOW_TOKENS

    def _model_supports_images(self, model: str) -> bool:
        """input_modalities of this model from /models (deepseek-flash: text+image)."""
        entry = self._entry_for(model)
        return bool(entry and entry.get("vision"))

    @staticmethod
    def _has_image_content(messages: List[Dict]) -> bool:
        """True if any message carries image content (Ollama 'images' or OpenAI image_url)."""
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("images"):
                return True
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        return False

    def _vision_error(self, model: str) -> VisionNotAvailableError:
        """Typed, client-renderable error for image input a DeepSeek model cannot accept."""
        return VisionNotAvailableError(
            "I'm sorry, but vision is not available for this DeepSeek model: its /models "
            "entry lists no image input modality, so it cannot see images. Send text only, "
            "pick a model that reports image input (deepseek-flash), or switch "
            "config.SERVER_BACKEND back to \"llama\" with a vision-capable GGUF.",
            model=model,
        )

    def _deepseek_error(self, exc: Exception) -> Exception:
        """Re-word a llama.cpp-flavoured failure to talk about DeepSeek."""
        text = str(exc)
        if "DeepSeek" in text or "deepseek" in text:
            return exc
        reworded = text.replace("llama.cpp", "DeepSeek").replace("llama-server", "DeepSeek")
        if isinstance(exc, ConnectionError):
            reworded += (f" DeepSeek is a hosted API at {self._api_url} - the brain only "
                         f"attaches to it; check the key in config/deepseek_servers.json.")
        if isinstance(exc, (ConnectionError, TimeoutError, RuntimeError, FileNotFoundError)):
            return type(exc)(reworded)
        return exc

    # ---- health / models --------------------------------------------------

    async def health_check(self, force: bool = False) -> Tuple[bool, str, List[str]]:
        """DeepSeek health = GET /models answers 200 -> (healthy, error, model ids)."""
        import time
        if not force and self._is_healthy and (time.time() - self._last_check) < HEALTH_CHECK_INTERVAL_SEC:
            return True, "", [m["id"] for m in self._models]
        models = await self._models_cached(force=force)
        if models:
            self._is_healthy = True
            self._last_check = time.time()
            ids = [m["id"] for m in models]
            logger.info(f"DeepSeek is healthy. Available models: {ids}")
            return True, "", ids
        self._is_healthy = False
        error = (f"DeepSeek returned no models from {self._api_url}/models - "
                 "check the Bearer key in config/deepseek_servers.json ('api_key') "
                 "or config.DEEPSEEK_API_KEY.")
        logger.warning(error)
        return False, error, []

    async def get_models_list(self) -> List[Dict]:
        """Models served by DeepSeek, from /models - no disk scan.

        name is the routing id the CLIENT picks and sends back; size stays 0 (a hosted
        model has no file), ctx/max_output/vision/thinking describe it.
        """
        models = await self._models_cached(force=True)
        out = []
        for m in models:
            out.append({
                "name": m["id"],
                "folder": "deepseek",
                "size": 0,
                "on_disk": False,
                "loaded": False,
                "display_name": m["name"],
                "ctx": m["ctx"],
                "max_output": m["max_output"],
                "vision": m["vision"],
                "thinking": m["thinking"].describe(),
            })
        logger.info("[DEEPSEEK] Serving %d model(s): %s", len(out),
                    ", ".join(m["name"] for m in out) or "(none)")
        return out

    async def get_current_model(self, force: bool = False) -> Optional[str]:
        """The first model this endpoint serves (DeepSeek loads nothing per request)."""
        models = await self._models_cached(force=force)
        return models[0]["id"] if models else None

    # ---- OCR --------------------------------------------------------------

    async def ocr_transcribe(self, image_base64: str, ocr_models: List[str]) -> str:
        """OCR needs a dedicated llama.cpp OCR instance; DeepSeek runs none."""
        logger.warning("[DEEPSEEK] OCR request refused - the deepseek backend runs no OCR "
                       "model (asked for: %s)", ", ".join(ocr_models or []) or "none")
        return ("ERROR: OCR is not available with the deepseek backend. DeepSeek serves chat "
                "models only and has no dedicated OCR instance; switch config.SERVER_BACKEND "
                "to \"llama\" to use the OCR models from profiles.json, or use transcribe_image "
                "with a vision-capable DeepSeek model (deepseek-flash).")

    # ---- unload/reload ----------------------------------------------------

    def unload_model(self, model: str) -> None:
        """No-op: DeepSeek loads nothing per request; the brain owns no process."""
        logger.info("[DEEPSEEK] unload_model is a no-op - hosted API (%s)", model)

    def reload_model(self, model: str) -> None:
        """No-op: switching a DeepSeek model = sending its id, nothing is loaded."""
        logger.info("[DEEPSEEK] reload_model is a no-op - pick the model id in the request (%s)", model)

    # ---- status / error wording -------------------------------------------

    def status_message(self, error: Exception, model_name: str = None, timeout: int = None) -> str:
        """User-friendly wording for a DeepSeek failure (llama's status.py names llama.cpp)."""
        error_str = str(error).lower()
        if "401" in error_str or "credentials" in error_str or "unauthorized" in error_str:
            return (
                "WARNING: DeepSeek Rejected Our Credentials\n\n"
                "DeepSeek requires a Bearer key on every request.\n\n"
                "1. Put the key in config/deepseek_servers.json ('api_key'), or set\n"
                "   DEEPSEEK_API_KEY in config/config.py (the env variable works too)\n"
                "2. Check what the account serves: GET " + self._api_url + "/models\n\n"
                "Then send your message again."
            )
        if "connection" in error_str or "refused" in error_str or "not reachable" in error_str:
            return (
                "WARNING: DeepSeek Not Reachable\n\n"
                f"The hosted API at {self._api_url} did not answer. Please:\n\n"
                "1. Check the network / firewall\n"
                "2. Check the endpoint in config/deepseek_servers.json ('base_url')\n"
                "3. Check the key ('api_key') - a wrong key answers 401, not a refusal\n"
            )
        if "timeout" in error_str or "timed out" in error_str:
            time_info = f"({timeout} seconds)" if timeout else ""
            return (
                f"WARNING: DeepSeek Response Timeout\n\n"
                f"The model took too long to respond {time_info}\n\n"
                "1. The prompt may be near the model's context window\n"
                "2. A long thinking level (high/max) can take minutes\n"
                "3. Check logs for details"
            )
        if "model" in error_str and ("not found" in error_str or "not served" in error_str):
            return (
                f"WARNING: Model Not Found\n\n"
                f"'{model_name}' is not served by this DeepSeek account.\n\n"
                "Check what it serves: GET " + self._api_url + "/models (needs the key),\n"
                "or pick another model in the picker."
            )
        return (
            f"WARNING: DeepSeek Error\n\n"
            f"An unexpected error occurred: {str(error)}\n\n"
            "1. Check the endpoint in config/deepseek_servers.json\n"
            "2. Check the API key\n"
            "3. Check logs for details"
        )

    # ---- lifecycle --------------------------------------------------------

    async def start_server(self) -> bool:
        """Attach to every configured DeepSeek endpoint and expand it per served model.

        Builds the registry with kind="deepseek": each endpoint is probed with GET /models
        and becomes one routable backend per model id, so the queue has one group per model
        and the picker can switch between them. No process is ever started or killed.
        """
        from ..llama.backend_registry import LlmBackendRegistry, set_registry

        reg = LlmBackendRegistry.from_config(kind="deepseek")
        set_registry(reg)
        ok = await reg.start_all()
        if ok:
            reg.start_health_poller(interval_sec=HEALTH_CHECK_INTERVAL_SEC)
        else:
            logger.error(
                "[DEEPSEEK] No DeepSeek endpoint answered /models - check the key in "
                "config/deepseek_servers.json; chat stays refused until one answers"
            )
        return ok

    async def close(self) -> None:
        """Stop the registry poller (nothing is ever killed) and release the HTTP pool."""
        from ..llama.backend_registry import get_registry
        reg = get_registry()
        if reg is not None:
            try:
                reg.stop_health_poller()
                await reg.stop_all()   # attach-only backends: no process is terminated
            except Exception as e:
                logger.warning("[DEEPSEEK] Registry shutdown issue (ignored): %s", e)
        await close_async_client()
        logger.info("[DEEPSEEK] DeepSeekProvider closed - HTTP pool released")


def deepseek_headers_for(api_url: str) -> Optional[dict]:
    """Bearer headers for one DeepSeek endpoint.

    Key precedence: the 'api_key' of the deepseek_servers.json entry whose base_url matches
    *api_url*, then config.DEEPSEEK_API_KEY, then env DEEPSEEK_API_KEY. None when no key is set.
    """
    wanted = (api_url or "").strip().rstrip("/").lower()
    try:
        import config
        for inst in config.get_deepseek_server_instances():
            if inst.api_url.rstrip("/").lower() == wanted:
                if inst.api_key:
                    return deepseek_auth_headers(inst.api_key)
                break
        key = (getattr(config, "DEEPSEEK_API_KEY", "") or "").strip()
    except Exception as e:  # pragma: no cover - defensive: a broken config never blocks chat
        logger.debug("[DEEPSEEK] key lookup for %s failed: %s", api_url, e)
        key = ""
    if not key:
        key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    return deepseek_auth_headers(key)
