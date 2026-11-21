"""OpenAI STT provider (2026-07): POST /v1/audio/transcriptions.

Audio LEAVES the machine and costs per minute (~$0.006/min for the mini model).
The API key comes from addons.json (openai_api_key); without one the provider
reports a clear error instead of failing mysteriously.

Surface matches the local engine:
  transcribe(audio_bytes, fmt="webm") -> text
  close()
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger("COOLEMS")

_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
_MAX_BYTES = 25 * 1024 * 1024  # API limit: 25 MB per file


class OpenAISTT:
    """Stateless wrapper - one multipart HTTP call per transcribe()."""

    def __init__(self, cfg: Any):
        self._cfg = cfg

    def close(self) -> None:
        pass

    async def transcribe(self, audio_bytes: bytes, fmt: str = "webm") -> str:
        if not audio_bytes:
            raise ValueError("empty audio upload")
        if not self._cfg.openai_api_key:
            raise RuntimeError(
                "OpenAI STT needs an API key - set stt.openai_api_key in "
                "Settings > Addons (or use the local faster-whisper provider)")
        if len(audio_bytes) > _MAX_BYTES:
            raise ValueError(f"audio too large for the OpenAI API ({len(audio_bytes)} bytes)")

        language = None if self._cfg.language == "auto" else self._cfg.language
        payload = {
            "model": self._cfg.openai_model,
            "file": ("audio." + (fmt or "webm"), audio_bytes,
                     "audio/" + (fmt or "webm")),
        }
        if language:
            payload["language"] = language
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(
                _ENDPOINT,
                headers={"Authorization": f"Bearer {self._cfg.openai_api_key}"},
                data=payload,
            )
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI STT HTTP {r.status_code}: {r.text[:300]}")
        text = r.json().get("text", "").strip()
        logger.info("[STT] openai %s: %d bytes -> %d chars",
                    self._cfg.openai_model, len(audio_bytes), len(text))
        return text
