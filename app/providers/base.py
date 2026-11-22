"""Abstract base provider interface - every provider implementation conforms to it.

chat_stream() returns (response_str, TokenStats) so each call carries its own real token
counts; nothing per-user is stored in globals.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Tuple, Optional
import asyncio

from .token_stats import TokenStats


class BaseProvider(ABC):
    """Abstract base class for AI providers (llama.cpp, etc.)"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g. 'llama')."""
        ...

    @property
    @abstractmethod
    def api_url(self) -> str:
        """Base API URL."""
        ...

    @abstractmethod
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
        """Stream chat completions; returns (response_string, TokenStats) with real counts from
        the provider response, one TokenStats per call so concurrent users stay isolated.

        enable_thinking: bool (show reasoning or not) or a canonical level
        "none"/"low"/"medium"/"high". A level is translated into the serving model's own
        vocabulary by its ThinkingPolicy (app/providers/llama/thinking.py); a bool only decides
        whether reasoning is displayed.
        """
        ...

    @abstractmethod
    async def chat_non_streaming(
        self,
        model: str,
        messages: List[Dict],
        temperature: float = 0.7,
    ) -> str:
        """Non-streaming chat completion; returns the response string (no token stats).
        """
        ...

    @abstractmethod
    def build_payload(
        self,
        model: str,
        messages: List[Dict],
        *,
        stream: bool = True,
        temperature: float = 0.7,
        enable_thinking=None,
        tools: list = None,
    ) -> Dict:
        """Build the request payload for this provider.

        enable_thinking: bool (show reasoning) or a canonical level "none"/"low"/"medium"/"high".
        Implementations translate a level with the serving model's ThinkingPolicy; a model whose
        controls are unknown gets no thinking keys at all.
        """
        ...

    @abstractmethod
    def health_check(self, force: bool = False) -> Tuple[bool, str, List[str]]:
        """Check provider health.

        Args:
            force: If True, bypass cache and force a fresh health check.

        Returns:
            Tuple of (is_healthy, error_message, list_of_model_names).
        """
        ...

    @abstractmethod
    def get_models_list(self) -> List[Dict]:
        """Return list of available models.
        Each dict has at least {'name': str, 'size': int}.

        Returns:
            List of model dictionaries.
        """
        ...

    @abstractmethod
    def unload_model(self, model: str) -> None:
        """Unload a model from memory (for memory management during image generation).

        Args:
            model: Model name to unload.
        """
        ...

    @abstractmethod
    def reload_model(self, model: str) -> None:
        """Reload a model into memory.

        Args:
            model: Model name to reload.
        """
        ...

    @abstractmethod
    def status_message(self, error: Exception, model_name: str, timeout: int = None) -> str:
        """User-friendly error message for an exception, naming the model and optional timeout.
        """
        ...

    @abstractmethod
    async def ocr_transcribe(
        self,
        image_base64: str,
        ocr_models: List[str],
    ) -> str:
        """Transcribe text from an image using OCR.

        Args:
            image_base64: Base64-encoded image data.
            ocr_models: List of OCR model names to try in order.

        Returns:
            Extracted text or an error string.
        """
        ...

    @abstractmethod
    def start_server(self) -> bool:
        """Start the provider server process if not already running.

        Returns:
            True if server is running (was already running or just started),
            False if failed to start.
        """
        ...

    async def get_balance(self) -> Optional[Dict[str, Any]]:
        """Return the account balance for this provider, or None if the provider
        has no billable account (local models) or the info is unavailable.

        Default: None. Cloud providers (DeepSeek, OpenAI) override this with a
        free metadata GET (no tokens, no cost). Implementations should cache the
        result for at least 30 seconds so busy agentic turns do not hammer the API.

        Returns:
            Dict like {"currency": "CNY", "total": "110.00", ...} or None.
        """
        return None

    # Optional method for multi-instance providers
    def router_status(self) -> Optional[str]:
        """Return status summary of all instances (for multi-instance providers).
        Single-instance providers can return None or a simple status string.

        Returns:
            Status summary string or None.
        """
        return None
