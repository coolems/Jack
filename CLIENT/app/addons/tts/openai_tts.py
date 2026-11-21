"""OpenAI TTS provider (2026-07): POST /v1/audio/speech.

Audio LEAVES the machine and costs per character (~$15/1M chars). The API key comes
from addons.json (openai_api_key); without one the provider reports a clear error
instead of failing mysteriously.

Surface matches the local engine:
  speak(text, voice="", lang="") -> audio bytes (MP3 from the API)
  close()
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger("COOLEMS")

_ENDPOINT = "https://api.openai.com/v1/audio/speech"
#: The API accepts 4096 chars per call; split long text on sentence-ish boundaries.
_MAX_CHARS = 4000


class OpenAITts:
    """Stateless wrapper - nothing to load, one HTTP call per speak()."""

    def __init__(self, cfg: Any):
        self._cfg = cfg

    def close(self) -> None:
        pass

    async def speak(self, text: str, voice: str = "", lang: str = "") -> bytes:
        if not text or not text.strip():
            raise ValueError("no text to speak")
        if not self._cfg.openai_api_key:
            raise RuntimeError(
                "OpenAI TTS needs an API key - set tts.openai_api_key in "
                "Settings > Addons (or use the local Supertonic provider)")

        voice = voice or self._cfg.openai_voice
        chunks = [text[i:i + _MAX_CHARS] for i in range(0, len(text), _MAX_CHARS)]
        out = bytearray()
        async with httpx.AsyncClient(timeout=120) as client:
            for chunk in chunks:
                r = await client.post(
                    _ENDPOINT,
                    headers={"Authorization": f"Bearer {self._cfg.openai_api_key}"},
                    json={
                        "model": self._cfg.openai_model,
                        "input": chunk,
                        "voice": voice,
                        "response_format": "mp3",
                    },
                )
                if r.status_code != 200:
                    raise RuntimeError(f"OpenAI TTS HTTP {r.status_code}: {r.text[:300]}")
                out += r.content
        logger.info("[TTS] openai %s: %d char(s) -> %d bytes",
                    self._cfg.openai_model, len(text), len(out))
        return bytes(out)
