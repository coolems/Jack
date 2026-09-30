"""
TTS Engine — synthesis, playback orchestration, and pool management.

Pure asyncio implementation.  No threading module.
"""

import asyncio
import io
import logging
import os
from typing import Callable, Coroutine, List, Optional

from static.voice.config import TTSConfig, VoiceConfig
from static.voice.pool import SentencePool
from static.voice.player import AudioPlayer

logger = logging.getLogger("COOLEMS.Voice")


# ------------------------------------------------------------------
# TTS Manager
# ------------------------------------------------------------------

class TTSManager:
    """
    Main TTS manager — orchestrates synthesis, playback, and pool.

    Singleton (one instance per process).
    """

    _instance: Optional["TTSManager"] = None

    # -- singleton helpers ------------------------------------------

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    # -- init -------------------------------------------------------

    def __init__(
        self,
        config: Optional[TTSConfig] = None,
        _force_init: bool = False,
    ):
        if self._initialized and not _force_init:
            return

        self._initialized = True
        self._config = config or self._load_config()

        # Sub-systems
        self._pool = SentencePool(max_size=self._config.max_pool_size)
        self._player = AudioPlayer(sample_rate=self._config.sample_rate)

        # State
        self._running = False
        self._tasks: List[asyncio.Task] = []

        # Lazy locks — created on first async access so they bind to
        # the correct event loop (singleton may outlive a single loop).
        self._lock: Optional[asyncio.Lock] = None
        self._tts_lock: Optional[asyncio.Lock] = None
        self._restart_lock: Optional[asyncio.Lock] = None
        self._loop_id: Optional[int] = None

        # Supertonic engine (lazy-loaded)
        self._tts_engine = None

        # Callbacks
        self._on_sentence_start: Optional[Callable[[str], Coroutine]] = None
        self._on_sentence_end: Optional[Callable[[str], Coroutine]] = None
        self._on_pool_empty: Optional[Callable[[], Coroutine]] = None

        logger.info("TTSManager initialized (enabled=%s)", self._config.enabled)

    # -- lazy lock helpers ------------------------------------------

    def _get_current_loop_id(self) -> int:
        """Return id of the currently running event loop."""
        return id(asyncio.get_running_loop())

    async def _ensure_lock(self) -> asyncio.Lock:
        """
        Return a lock bound to the *current* event loop.

        Because TTSManager is a singleton, asyncio.Lock() created in
        __init__ would bind to whatever loop was active at that moment
        (possibly a different loop from the one actually using the
        manager).  This helper lazily creates / recreates locks so they
        always match the running loop.
        """
        loop_id = self._get_current_loop_id()
        if self._lock is None or self._loop_id != loop_id:
            self._loop_id = loop_id
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_tts_lock(self) -> asyncio.Lock:
        """Same lazy-creation pattern for the TTS-specific lock."""
        loop_id = self._get_current_loop_id()
        if self._tts_lock is None or self._loop_id != loop_id:
            self._loop_id = loop_id
            self._tts_lock = asyncio.Lock()
        return self._tts_lock

    async def _ensure_restart_lock(self) -> asyncio.Lock:
        """Lazy-create restart lock bound to current event loop."""
        loop_id = self._get_current_loop_id()
        if self._restart_lock is None or self._loop_id != loop_id:
            self._loop_id = loop_id
            self._restart_lock = asyncio.Lock()
        return self._restart_lock

    # -- properties -------------------------------------------------

    @property
    def config(self) -> TTSConfig:
        return self._config

    @property
    def pool(self) -> SentencePool:
        return self._pool

    @property
    def is_running(self) -> bool:
        return self._running

    # -- config loading ---------------------------------------------

    def _load_config(self) -> TTSConfig:
        """Load configuration from config.py if available."""
        try:
            from config import (  # type: ignore
                TTS_ENABLED,
                TTS_AUTO_ENABLE,
                TTS_VOICE1_NAME,
                TTS_VOICE1_SPEED,
                TTS_VOICE1_ENABLED,
                TTS_VOICE2_NAME,
                TTS_VOICE2_SPEED,
                TTS_VOICE2_ENABLED,
                TTS_ACTIVE_VOICE,
                TTS_MAX_POOL_SIZE,
                TTS_PRE_RENDER_SLOTS,
                TTS_AUDIO_DIR,
                TTS_MODEL,
                TTS_TOTAL_STEPS,
                TTS_SILENCE_DURATION,
                TTS_LANG,
            )
            return TTSConfig(
                enabled=TTS_ENABLED,
                auto_enable=TTS_AUTO_ENABLE,
                voice1_name=TTS_VOICE1_NAME,
                voice1_speed=TTS_VOICE1_SPEED,
                voice1_enabled=TTS_VOICE1_ENABLED,
                voice2_name=TTS_VOICE2_NAME,
                voice2_speed=TTS_VOICE2_SPEED,
                voice2_enabled=TTS_VOICE2_ENABLED,
                active_voice=TTS_ACTIVE_VOICE,
                max_pool_size=TTS_MAX_POOL_SIZE,
                audio_dir=TTS_AUDIO_DIR,
                model=TTS_MODEL,
                total_steps=TTS_TOTAL_STEPS,
                silence_duration=TTS_SILENCE_DURATION,
                lang=TTS_LANG,
            )
        except ImportError:
            logger.debug("No external config module found, using defaults")
            return TTSConfig()

    def reload_config(self) -> None:
        """Reload configuration from config.py."""
        self._config = self._load_config()
        logger.info("TTS configuration reloaded")

    # -- voice selection --------------------------------------------

    def get_active_voice_config(self) -> VoiceConfig:
        """Return VoiceConfig for the currently active voice."""
        if self._config.active_voice == 2 and self._config.voice2_enabled:
            return VoiceConfig(
                voice_name=self._config.voice2_name,
                speed=self._config.voice2_speed,
                total_steps=self._config.total_steps,
                silence_duration=self._config.silence_duration,
                lang=self._config.lang,
            )
        return VoiceConfig(
            voice_name=self._config.voice1_name,
            speed=self._config.voice1_speed,
            total_steps=self._config.total_steps,
            silence_duration=self._config.silence_duration,
            lang=self._config.lang,
        )

    # -- Supertonic engine (lazy load) ------------------------------

    def _get_tts_engine(self):
        """Get or create the Supertonic TTS engine."""
        if self._tts_engine is not None:
            return self._tts_engine

        from supertonic import TTS  # noqa

        logger.info("Loading Supertonic model: %s", self._config.model)
        self._tts_engine = TTS(model=self._config.model)
        logger.info(
            "Supertonic loaded. Voices: %s, "
            "Sample rate: %s, "
            "Multilingual: %s",
            self._tts_engine.voice_style_names,
            self._tts_engine.sample_rate,
            self._tts_engine.is_multilingual,
        )
        return self._tts_engine

    # -- synthesis (shared core) ------------------------------------

    async def _synthesize_waveform(
        self,
        text: str,
        voice_config: Optional[VoiceConfig] = None,
    ):
        """
        Core synthesis — returns (waveform, duration).

        Runs blocking ML inference in executor so the event loop stays
        responsive.  Shared by synthesize_to_file() and synthesize_to_bytes().
        """
        vc = voice_config or self.get_active_voice_config()

        def _synth():
            engine = self._get_tts_engine()
            voice_style = engine.get_voice_style(vc.voice_name)
            waveform, duration = engine.synthesize(
                text=text,
                voice_style=voice_style,
                total_steps=vc.total_steps,
                speed=vc.speed,
                silence_duration=vc.silence_duration,
                lang=vc.lang,
            )
            return waveform, duration

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _synth)

    async def synthesize_to_file(
        self,
        text: str,
        output_path: str,
        voice_config: Optional[VoiceConfig] = None,
    ) -> Optional[str]:
        """
        Synthesize text to a WAV file.
        Returns the output path on success, None on failure.
        """
        try:
            # Ensure target directory exists
            target_dir = os.path.dirname(output_path)
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)

            waveform, duration = await self._synthesize_waveform(text, voice_config)
            engine = self._get_tts_engine()
            engine.save_audio(waveform, output_path)
            logger.debug(
                "Synthesized '%s...' -> %s (%.2fs)",
                text[:50], output_path, duration[0],
            )
            return output_path
        except Exception as e:
            logger.error("Synthesis failed for '%s...': %s", text[:50], e)
            return None

    async def synthesize_to_bytes(
        self,
        text: str,
        voice_config: Optional[VoiceConfig] = None,
    ) -> Optional[bytes]:
        """
        Synthesize text to WAV bytes.
        Returns WAV bytes on success, None on failure.
        """
        try:
            waveform, _duration = await self._synthesize_waveform(text, voice_config)
            engine = self._get_tts_engine()

            buffer = io.BytesIO()
            try:
                import soundfile as sf  # noqa
                sf.write(buffer, waveform.squeeze(), engine.sample_rate, format="WAV")
            except ImportError:
                logger.debug("Non-critical exception caught at CLIENT/static/voice/engine.py:297")
                buffer = io.BytesIO()
                import wave as wave_mod

                with wave_mod.open(buffer, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(engine.sample_rate)
                    audio_data = (waveform.squeeze() * 32767).astype("int16")
                    wf.writeframes(audio_data.tobytes())

            return buffer.getvalue()
        except Exception as e:
            logger.error("Synthesis to bytes failed for '%s...': %s", text[:50], e)
            return None

    # -- public API -------------------------------------------------

    async def add_text(self, text: str) -> int:
        """
        Add text to the sentence pool.
        Returns number of sentences added.
        """
        if not text or not text.strip():
            return 0
        count = await self._pool.add(text)
        logger.debug(
            "Added %d sentence(s) to pool. Pool size: %d",
            count, await self._pool.size(),
        )
        return count

    async def add_sentence(self, sentence: str) -> bool:
        """Add a single sentence to the pool."""
        success = await self._pool.add_sentence(sentence)
        if success:
            logger.debug("Added sentence to pool. Pool size: %d", await self._pool.size())
        return success

    async def clear_pool(self) -> int:
        """Clear all sentences from the pool."""
        count = await self._pool.clear()
        logger.info("Cleared %d sentence(s) from pool", count)
        return count

    async def stop(self) -> None:
        """Stop TTS playback and clear pool immediately."""
        logger.info("Stopping TTS...")
        self._running = False

        # Cancel async tasks first
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()

        # Delegate audio stop to the player (single source of truth)
        await self._player.stop()

        # Clear the pool
        try:
            await self._pool.clear()
        except Exception:
            logger.debug("Cleanup/delete non-critical error at CLIENT/static/voice/engine.py:360")

        logger.info("TTS stopped")

    async def get_status(self) -> dict:
        """Get current TTS status."""
        return {
            "running": self._running,
            "playing": await self._player.is_playing(),
            "pool_size": await self._pool.size(),
            "active_voice": self._config.active_voice,
            "voice_name": self.get_active_voice_config().voice_name,
            "auto_enable": self._config.auto_enable,
        }

    # -- playback loop ----------------------------------------------

    async def start(self) -> None:
        """
        Start the TTS playback loop.
        Runs in background, processing sentences from the pool.
        """
        if self._running:
            return

        self._running = True
        logger.info("TTS playback loop started")

        task = asyncio.create_task(self._playback_loop())
        self._tasks.append(task)

    async def restart(self) -> None:
        """
        Restart the TTS playback loop after a stop.
        CRITICAL FIX: Uses a lock to prevent concurrent restarts.
        Multiple /api/tts/add calls can arrive simultaneously, and without
        this lock they would all call restart() at once, causing:
        - Multiple stop() calls cancelling each other's tasks
        - Race conditions on _running flag
        - Playback cancelled during synthesis errors
        """
        restart_lock = await self._ensure_restart_lock()
        async with restart_lock:
            # Double-check: another request may have already restarted
            if self._running and self._tasks and not self._tasks[0].done():
                logger.debug("TTS already running, skipping restart")
                return

            logger.info("Restarting TTS...")
            await self.stop()
            self._running = True
            self._tasks = []
            task = asyncio.create_task(self._playback_loop())
            self._tasks.append(task)
            logger.info("TTS restarted")

    async def _playback_loop(self) -> None:
        """Main playback loop — reads from pool and plays audio."""
        while self._running:
            try:
                # Wait for a sentence (non-blocking, pure asyncio)
                sentence = await self._pool.get(timeout=0.5)
                if not sentence:
                    continue

                if not self._running:
                    break

                # Synthesize and play on-demand
                logger.debug("Synthesizing on-demand: %s...", sentence[:50])
                try:
                    wav_bytes = await self.synthesize_to_bytes(sentence)
                    if wav_bytes and self._running:
                        if self._on_sentence_start:
                            await self._on_sentence_start(sentence)
                        await self._player.play_wav_bytes(wav_bytes)
                        if self._on_sentence_end:
                            await self._on_sentence_end(sentence)
                except asyncio.CancelledError:
                    logger.info("Playback cancelled during synthesis")
                    break
                except Exception as e:
                    logger.error("Synthesis/playback error: %s", e)

            except asyncio.CancelledError:
                logger.info("Playback loop cancelled")
                break
            except Exception as e:
                logger.error("Playback loop error: %s", e)
                await asyncio.sleep(0.1)

    # -- callbacks --------------------------------------------------

    def on_sentence_start(self, callback):
        """Set callback for when a sentence starts playing."""
        self._on_sentence_start = callback

    def on_sentence_end(self, callback):
        """Set callback for when a sentence finishes playing."""
        self._on_sentence_end = callback

    def on_pool_empty(self, callback):
        """Set callback for when the pool becomes empty."""
        self._on_pool_empty = callback

    # -- singleton reset (for testing) ------------------------------

    @classmethod
    async def reset_instance(cls) -> None:
        """Reset the singleton instance (for testing / CLI reuse)."""
        if cls._instance:
            await cls._instance.stop()
            cls._instance = None
