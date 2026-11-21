"""STT providers - one module per engine, make_engine() routes by config.provider.

  faster_whisper/ - local, free, private (auto-detects EN/DE/RO, zero config)
  openai_stt.py   - OpenAI gpt-4o-mini-transcribe (audio leaves the machine)

Every provider exposes the same async surface:
  transcribe(audio_bytes, fmt="webm") -> text
  close()
"""

from typing import Any


async def make_engine(cfg: Any) -> Any:
    """Build the active STT provider for the validated SttConfig."""
    if cfg.provider == "faster_whisper":
        from app.addons.stt.faster_whisper.engine import FasterWhisperEngine
        eng = FasterWhisperEngine(cfg)
        await eng.load()
        return eng
    from app.addons.stt.openai_stt import OpenAISTT
    return OpenAISTT(cfg)
