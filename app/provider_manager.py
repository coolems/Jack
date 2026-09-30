"""
Global provider manager singleton.
Allows any module (including standalone tool functions) to access the active provider.

DELIVERY NOTE (2026-08-16): This file is a process-local startup singleton, so it
exists on BOTH sides by design -- CLIENT needs it at boot time BEFORE any WebSocket
connection exists (chicken-and-egg: init imports it before tools can be fetched).
In addition the SERVER delivers its copy to each client via shared_sources under
the key 'app.provider_manager', so delivered TOOL code that does
'from app.provider_manager import ProviderManager' resolves to the server-served
source. The two copies must stay byte-identical (it is a stateless holder).
"""

from typing import List, Optional
from app.providers import BaseProvider


class ProviderManager:
    """Singleton that holds the active provider instance."""

    _provider: Optional[BaseProvider] = None
    _ocr_models: List[str] = []

    @classmethod
    def set_provider(cls, provider: BaseProvider) -> None:
        """Register the active provider instance (called once at boot)."""
        cls._provider = provider

    @classmethod
    def get_provider(cls) -> Optional[BaseProvider]:
        """Return the active provider, or None before boot has set one."""
        return cls._provider

    @classmethod
    def set_ocr_models(cls, models: List[str]) -> None:
        """Store the model names available for OCR transcription."""
        cls._ocr_models = models

    @classmethod
    def get_ocr_models(cls) -> List[str]:
        """Return the configured OCR model names (empty list if unset)."""
        return cls._ocr_models
