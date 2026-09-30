"""
Main LlamaProvider class - thin orchestrator that delegates to submodules.

TOKEN STATISTICS:
    The chat_stream() method now returns a tuple of (response_str, TokenStats)
    instead of just a string. The TokenStats contains real token counts from
    llama.cpp's final SSE response chunk. This is bulletproof for concurrent users.

HTTP CLIENT POOL:
    Shared httpx.AsyncClient and requests.Session are managed in http_client.py.
    LlamaProvider calls close() on shutdown to release pooled connections.
"""
import asyncio
import logging
from typing import List, Dict, Any, Tuple, Optional

from config import HEALTH_CHECK_INTERVAL_SEC, PROVIDER_DEFAULT_TIMEOUT

from .payload import build_llama_payload
from .streaming import chat_stream as _chat_stream
from .non_streaming import chat_non_streaming as _chat_non_streaming
from .health import HealthChecker
from .server import start_server as _start_server
from .server import reload_with_model as _reload_with_model
from .server import get_current_model as _get_current_model
from .ocr import ocr_transcribe as _ocr_transcribe
from .status import status_message as _status_message
from .http_client import close_async_client, close_sync_session

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
        """Gracefully shut down shared HTTP clients. Idempotent.
        
        (2026-09-08 multi-chat) stops the backend registry first: health poller cancelled,
        locally-spawned llama-server instances terminated, then the HTTP pool is released."""
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
        close_sync_session()
        logger.info("LlamaProvider closed — HTTP pool released")

    # ---- chat_stream ----
    async def chat_stream(
        self,
        model: str,
        messages: List[Dict],
        websocket: Any,
        temperature: float = 0.7,
        enable_thinking: bool = False,
        conv_id: str = None,
        tools: list = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Tuple[str, TokenStats]:
        """
        Stream chat completions from llama.cpp.

        Returns a tuple of (response_string, TokenStats) where TokenStats
        contains real token counts from llama.cpp's final SSE response chunk.

        Args:
            model: Model name to use.
            messages: List of message dicts with 'role' and 'content'.
            websocket: FastAPI WebSocket for streaming content chunks.
            temperature: Sampling temperature (default 0.7).
            enable_thinking: Whether to enable thinking/reasoning mode.
            conv_id: Conversation ID for logging and stop control.
            tools: Optional list of tool definitions for native tool calling.
            stop_event: asyncio.Event set by the stop endpoint when user
                       clicks the red stop button. Checked on every SSE chunk.

        Returns:
            Tuple of (response_string, TokenStats).
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
        )

    # ---- chat_non_streaming ----
    async def chat_non_streaming(
        self,
        model: str,
        messages: List[Dict],
        temperature: float = 0.7,
    ) -> str:
        """Non-streaming chat completion. Returns the response string."""
        return await _chat_non_streaming(
            api_url=self._api_url,
            timeout=self._timeout,
            model=model,
            messages=messages,
            temperature=temperature,
        )

    # ---- build_payload ----
    def build_payload(
        self,
        model: str,
        messages: List[Dict],
        *,
        stream: bool = True,
        temperature: float = 0.7,
        tools: list = None,
    ) -> Dict:
        """
        Build the request payload for llama.cpp (OpenAI-compatible).

        Note: Messages should already be converted for vision format before
        calling this. Use converter.convert_messages_for_vision() if needed.
        if you have Ollama-style messages with 'images' keys.
        """
        return build_llama_payload(
            model, messages, stream=stream, temperature=temperature, tools=tools,
        )

    # ---- health_check ----
    async def health_check(self, force: bool = False) -> Tuple[bool, str, List[str]]:
        """Check llama.cpp health. Returns (is_healthy, error_message, model_names).

        (2026-09 threadless refactor): async - awaits the HealthChecker's pooled httpx probe
        on the caller's loop instead of blocking it with a sync requests call."""
        return await self._health.health_check(force=force)

    # ---- get_models_list ----
    async def get_models_list(self) -> List[Dict]:
        """Return list of ALL available models from disk (llama_server/models/*.gguf).

        Each dict has:
          - name: the model filename
          - size: file size in bytes
          - on_disk: True
          - loaded: True if currently loaded in server

        (2026-09 threadless refactor): async - disk scan runs off-loop, /v1/models probe uses
        the shared httpx pool."""
        return await self._health.get_models_list()

    # ---- switch_model ----
    async def switch_model(self, model_filename: str) -> Tuple[bool, str]:
        """Stop the current llama-server and restart with a different model.

        Args:
            model_filename: The .gguf filename to load.

        Returns:
            Tuple of (success, message).

        (2026-09 threadless refactor): async - the reload wait loop polls /health on the shared
        httpx pool with await asyncio.sleep() between attempts; only true OS leaves (process
        terminate/wait) run via asyncio.to_thread.
        """
        # (2026-09-08 multi-chat) reload EVERY local instance with the new model; remotes are
        # verified afterwards and marked unhealthy on mismatch. Falls back to the legacy
        # single-instance reload when no registry exists yet (e.g. direct LlamaProvider use).
        from . import backend_registry
        reg = backend_registry.get_registry()
        if reg is not None and len(reg.backends) > 0:
            return await reg.switch_model(model_filename)
        return await _reload_with_model(self._api_url, model_filename)

    # ---- get_current_model ----
    async def get_current_model(self, force: bool = False) -> Optional[str]:
        """Get the currently loaded model name.

        force=True bypasses the fresh in-memory cache and verifies via /v1/models
        (used right after a switch so the new model is reported immediately).

        (2026-09 threadless refactor): async - the /v1/models verification uses the shared httpx
        pool; the fresh-cache fast path stays a zero-HTTP in-memory read."""
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

    # ---- start_server ----
    async def start_server(self) -> bool:
        """Start the llama.cpp server process if not already running.

        (2026-09 threadless refactor): async - the startup health wait polls on the shared httpx
        pool with await asyncio.sleep(); Popen is non-blocking, pipe drainers stay daemon threads
        (documented Phase C leaf)."""
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
