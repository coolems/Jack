"""LlamaProvider - thin orchestrator delegating to the llama submodules.

chat_stream() returns (response_str, TokenStats) with the real counts from llama.cpp's
final SSE chunk. The shared httpx.AsyncClient / requests.Session live in http_client.py;
close() releases them on shutdown.
"""
import asyncio
import logging
from typing import List, Dict, Any, Tuple, Optional

from config import HEALTH_CHECK_INTERVAL_SEC, PROVIDER_DEFAULT_TIMEOUT

from .payload import build_llama_payload
from .streaming import chat_stream as _chat_stream, _thinking_policy_for
from .thinking import normalize_mode
from .non_streaming import chat_non_streaming as _chat_non_streaming
from .health import HealthChecker
from .server import get_current_model as _get_current_model
from .ocr import ocr_transcribe as _ocr_transcribe
from .status import status_message as _status_message
from .http_client import close_async_client

from ..base import BaseProvider
from ..token_stats import TokenStats

logger = logging.getLogger("COOLEMS.Provider.Llama")


class LlamaProvider(BaseProvider):
    """llama.cpp API provider (OpenAI-compatible)."""

    def __init__(self, api_url: str, timeout: int = PROVIDER_DEFAULT_TIMEOUT):
        self._api_url = api_url
        self._timeout = timeout
        self._health = HealthChecker(api_url, HEALTH_CHECK_INTERVAL_SEC)

    @property
    def name(self) -> str:
        return "llama"

    @property
    def api_url(self) -> str:
        return self._api_url

    # ---- Lifecycle --------------------------------------------------------

    async def close(self) -> None:
        """Release provider resources.

        Stops the backend registry first (health poller cancelled, locally-spawned llama-server
        processes terminated), then closes the shared HTTP pool.
        """
        # (2026-09-08 multi-chat) stop all backends before releasing the shared HTTP pool.
        from . import backend_registry
        _reg = backend_registry.get_registry()
        if _reg is not None:
            try:
                _reg.stop_health_poller()
                await _reg.stop_all()
            except Exception as e:
                logger.warning(f"Registry shutdown issue (ignored): {e}")
        await close_async_client()
        logger.info("LlamaProvider closed — HTTP pool released")

    # ---- chat_stream ----
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
        """Stream chat completions from llama.cpp; returns (response_string, TokenStats) with the
        real counts from the final SSE chunk.

        enable_thinking: bool (show reasoning) or a canonical level "none"/"low"/"medium"/"high";
        a level is translated into this model's vocabulary by its ThinkingPolicy and sent as
        chat_template_kwargs (see streaming._resolve_thinking).
        """
        return await _chat_stream(
            api_url=self._api_url,
            timeout=self._timeout,
            model=model,
            messages=messages,
            websocket=websocket,
            temperature=temperature,
            enable_thinking=enable_thinking,
            conv_id=conv_id,
            tools=tools,
            stop_event=stop_event,
            context_window=context_window,
        )

    # ---- chat_non_streaming ----
    async def chat_non_streaming(
        self,
        model: str,
        messages: List[Dict],
        temperature: float = 0.7,
        enable_thinking=None,
    ) -> str:
        """Non-streaming chat completion; returns the response string. enable_thinking accepts a
        canonical thinking level string (see chat_stream).
        """
        return await _chat_non_streaming(
            api_url=self._api_url,
            timeout=self._timeout,
            model=model,
            messages=messages,
            temperature=temperature,
            thinking_mode=enable_thinking if isinstance(enable_thinking, str) else None,
        )

    # ---- build_payload ----
    def build_payload(
        self,
        model: str,
        messages: List[Dict],
        *,
        stream: bool = True,
        temperature: float = 0.7,
        enable_thinking=None,
        tools: list = None,
        thinking_kwargs: Dict = None,
    ) -> Dict:
        """Build the llama.cpp (OpenAI-compatible) request payload.

        Messages must already be in vision format (converter.convert_messages_for_vision for
        Ollama-style 'images' keys). enable_thinking is a bool or a canonical level; a level is
        translated by this endpoint's ThinkingPolicy into chat_template_kwargs. An explicit
        thinking_kwargs overrides that translation; anything else sends no thinking keys.
        """
        if thinking_kwargs is None and isinstance(enable_thinking, str):
            mode = normalize_mode(enable_thinking)
            if mode:
                thinking_kwargs = _thinking_policy_for(self._api_url, model).kwargs(mode) or None
        return build_llama_payload(
            model, messages, stream=stream, temperature=temperature, tools=tools,
            thinking_kwargs=thinking_kwargs,
        )

    # ---- health_check ----
    async def health_check(self, force: bool = False) -> Tuple[bool, str, List[str]]:
        """llama.cpp health check -> (is_healthy, error_message, model_names), via the pooled
        httpx probe on the caller's loop.
        """
        return await self._health.health_check(force=force)

    # ---- get_models_list ----
    async def get_models_list(self) -> List[Dict]:
        """All models on disk (llama_server/models/*.gguf): name, size, on_disk, loaded.

        Disk scan runs off-loop; the /v1/models probe uses the shared httpx pool.
        """
        return await self._health.get_models_list()

    # ---- switch_model (REMOVED 2026-10 fleet) ----
    # Nodes are started with a fixed model by their node launcher; the brain routes each
    # request to the model group it names. There is no reload/switch path any more.

    # ---- get_current_model ----
    async def get_current_model(self, force: bool = False) -> Optional[str]:
        """Currently loaded model name. force=True skips the in-memory cache and verifies via
        /v1/models (used right after a switch).
        """
        return await _get_current_model(self._api_url, force=force)

    # ---- unload/reload ----
    def unload_model(self, model: str) -> None:
        """Unload a model from llama.cpp memory (no-op in single-model mode)."""
        logger.info(f"llama.cpp: unload_model is a no-op in single-model mode (model: {model})")

    def reload_model(self, model: str) -> None:
        """Reload a model into llama.cpp memory (no-op in single-model mode)."""
        logger.info(f"llama.cpp: reload_model is a no-op in single-model mode (model: {model})")

    # ---- status_message ----
    def status_message(self, error: Exception, model_name: str = None, timeout: int = None) -> str:
        """Generate user-friendly error message from an exception."""
        return _status_message(error, model_name=model_name, timeout=timeout)

    # ---- ocr_transcribe ----
    async def ocr_transcribe(self, image_base64: str, ocr_models: List[str]) -> str:
        """Transcribe text from an image using OCR via llama.cpp."""
        return await _ocr_transcribe(
            api_url=self._api_url,
            image_base64=image_base64,
            ocr_models=ocr_models,
        )

    # ---- tokenize ----
    async def tokenize(self, text: str) -> Optional[int]:
        """Exact token count of *text* using the loaded model's tokenizer (/tokenize).

        Returns the number of tokens llama.cpp assigns to the string, or None when the
        tokenizer is unreachable (backend down, or a non-llama backend without /tokenize).
        Callers fall back to a chars/4 estimate only on None.
        """
        if not text:
            return 0
        from .http_client import get_async_client
        try:
            client = await get_async_client()
            resp = await client.post(
                f"{self._api_url}/tokenize",
                json={"content": text},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return None
            tokens = resp.json().get("tokens")
            return len(tokens) if isinstance(tokens, list) else None
        except Exception:
            return None

    # ---- start_server ----
    async def start_server(self) -> bool:
        """Start the llama.cpp server process if not already running.

        Async: the startup health wait polls on the shared httpx
        pool with await asyncio.sleep(); Popen is non-blocking, pipe drainers stay daemon
        threads (documented Phase C leaf).
        """
        # (2026-09-08 multi-chat) the live instance list comes from config/llama_servers.json.
        # Build the registry, start/attach every configured instance and keep a health poller
        # running so unreachable remotes degrade gracefully instead of breaking chat routing.
        from . import backend_registry
        reg = backend_registry.LlmBackendRegistry.from_config()
        backend_registry.set_registry(reg)
        ok = await reg.start_all()
        if ok:
            reg.start_health_poller(interval_sec=HEALTH_CHECK_INTERVAL_SEC)
        return ok
