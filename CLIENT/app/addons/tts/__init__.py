"""TTS providers - one module per engine, make_engine() routes by config.provider.

  supertonic/  - local, free, private (relocated from the old static/voice prototype)
  openai_tts.py - OpenAI gpt-4o-mini-tts (audio leaves the machine, per-character cost)

Every provider exposes the same async surface:
  speak(text, voice="", lang="") -> WAV bytes
  close()
"""

import asyncio
from typing import Any


async def make_engine(cfg: Any) -> Any:
    """Build the active TTS provider for the validated TtsConfig."""
    if cfg.provider == "supertonic":
        from app.addons.tts.supertonic.engine import SupertonicEngine
        eng = SupertonicEngine(cfg)
        await eng.load()
        return eng
    from app.addons.tts.openai_tts import OpenAITts
    return OpenAITts(cfg)
