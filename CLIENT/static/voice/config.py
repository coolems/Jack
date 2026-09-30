"""
TTS Configuration Dataclasses.

VoiceConfig  — per-voice settings (name, speed, quality, etc.)
TTSConfig    — global TTS settings (pool size, model, voices, etc.)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class VoiceConfig:
    """Configuration for a single voice instance."""

    voice_name: str = "M1"           # Voice style: M1, M2, F1, F2, etc.
    speed: float = 1.05              # Speech speed multiplier (0.7-2.0)
    total_steps: int = 10            # Synthesis quality steps (1-100)
    silence_duration: float = 0.3    # Silence between chunks (seconds)
    lang: str = "en"                 # Language code


@dataclass
class TTSConfig:
    """Global TTS configuration."""

    # Voice 1 (primary)
    voice1_name: str = "M1"
    voice1_speed: float = 1.05
    voice1_enabled: bool = False

    # Voice 2 (secondary)
    voice2_name: str = "F1"
    voice2_speed: float = 1.05
    voice2_enabled: bool = False

    # Active voice (1 or 2)
    active_voice: int = 1

    # General settings
    enabled: bool = False
    auto_enable: bool = False        # Auto-speak incoming text
    max_pool_size: int = 100         # Max sentences in pool
    audio_dir: str = ""   # Directory for audio files (empty = current dir)
    model: str = "supertonic-2"      # Model name
    total_steps: int = 10            # Default synthesis quality
    silence_duration: float = 0.3    # Silence between chunks
    lang: str = "en"                 # Default language
    sample_rate: int = 24000         # Audio sample rate
