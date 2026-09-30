"""
Abstract base provider interface.
All provider implementations must conform to this interface.

TOKEN STATISTICS:
    The chat_stream() method now returns a tuple of (response_str, TokenStats)
    instead of just a string. This ensures that every provider call returns
    real token counts for the UI to display.
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
        enable_thinking: bool = False,
        conv_id: str = None,
        tools: list = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Tuple[str, TokenStats]:
        """
        Stream chat completions from the provider.

        IMPORTANT: Returns a tuple of (response_string, TokenStats) where
        TokenStats contains REAL token counts from the provider response.
        This is bulletproof for concurrent users because each call gets
        its own TokenStats — no shared globals.

        Args:
            model: Model name to use.
            messages: List of message dicts with 'role' and 'content'.
            websocket: FastAPI WebSocket for streaming content chunks.
            temperature: Sampling temperature (default 0.7).
            enable_thinking: Whether to enable thinking/reasoning mode.
            conv_id: Conversation ID for logging and stop control.
            tools: Optional list of tool definitions for native tool calling.
            stop_event: asyncio.Event that is set when user clicks stop button.
                       The streaming loop checks this on every chunk to abort early.

        Returns:
            Tuple of (response_string, TokenStats).
            - response_string: The full response text, or JSON string for tool calls
            - TokenStats: Token statistics including prompt_tokens, generated_tokens,
              generation_speed, and tokens_remaining
        """
        ...

    @abstractmethod
    async def chat_non_streaming(
        self,
        model: str,
        messages: List[Dict],
        temperature: float = 0.7,
    ) -> str:
        """
        Non-streaming chat completion.

        Returns the response string only (no token stats needed for non-streaming).

        Args:
            model: Model name to use.
            messages: List of message dicts with 'role' and 'content'.
            temperature: Sampling temperature (default 0.7).

        Returns:
            The full response string.
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
        enable_thinking: bool = False,
        tools: list = None,
    ) -> Dict:
        """
        Build the request payload for this provider.

        Args:
            model: Model name to use.
            messages: List of message dicts.
            stream: Whether to enable streaming.
            temperature: Sampling temperature.
            enable_thinking: Whether to enable thinking mode.
            tools: Optional list of tool definitions.

        Returns:
            Dictionary payload ready for HTTP request.
        """
        ...

    @abstractmethod
    def health_check(self, force: bool = False) -> Tuple[bool, str, List[str]]:
        """
        Check provider health.

        Args:
            force: If True, bypass cache and force a fresh health check.

        Returns:
            Tuple of (is_healthy, error_message, list_of_model_names).
        """
        ...

    @abstractmethod
    def get_models_list(self) -> List[Dict]:
        """
        Return list of available models.
        Each dict has at least {'name': str, 'size': int}.

        Returns:
            List of model dictionaries.
        """
        ...

    @abstractmethod
    def unload_model(self, model: str) -> None:
        """
        Unload a model from memory (for memory management during image generation).

        Args:
            model: Model name to unload.
        """
        ...

    @abstractmethod
    def reload_model(self, model: str) -> None:
        """
        Reload a model into memory.

        Args:
            model: Model name to reload.
        """
        ...

    @abstractmethod
    def status_message(self, error: Exception, model_name: str, timeout: int = None) -> str:
        """
        Generate user-friendly error message from an exception.

        Args:
            error: The exception that occurred.
            model_name: Name of the model that was being used.
            timeout: Optional timeout value to include in message.

        Returns:
            User-friendly error message string.
        """
        ...

    @abstractmethod
    async def ocr_transcribe(
        self,
        image_base64: str,
        ocr_models: List[str],
    ) -> str:
        """
        Transcribe text from an image using OCR.

        Args:
            image_base64: Base64-encoded image data.
            ocr_models: List of OCR model names to try in order.

        Returns:
            Extracted text or an error string.
        """
        ...

    @abstractmethod
    def start_server(self) -> bool:
        """
        Start the provider server process if not already running.

        Returns:
            True if server is running (was already running or just started),
            False if failed to start.
        """
        ...

    # Optional method for multi-instance providers
    def router_status(self) -> Optional[str]:
        """
        Return status summary of all instances (for multi-instance providers).
        Single-instance providers can return None or a simple status string.

        Returns:
            Status summary string or None.
        """
        return None
