"""
Voice module — TTS (Text-to-Speech) system using Supertonic.

Modules:
  config      — VoiceConfig, TTSConfig dataclasses
  text_utils  — sanitize_text, split_into_sentences
  pool        — SentencePool (async-safe queue)
  player      — AudioPlayer (pygame + asyncio)
  engine      — TTSManager (orchestration)
  cli         — CLI entry point

Provides:
  - Standalone CLI operation
  - Importable API for COOLEMS integration
  - Sentence pool management
  - Multi-voice support
"""

from static.voice.config import TTSConfig, VoiceConfig
from static.voice.engine import TTSManager
from static.voice.pool import SentencePool
from static.voice.player import AudioPlayer

__all__ = [
    "TTSManager",
    "TTSConfig",
    "VoiceConfig",
    "SentencePool",
    "AudioPlayer",
]
