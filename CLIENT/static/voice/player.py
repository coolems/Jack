"""
Cross-platform audio player using pygame with pure asyncio.

All playback is non-blocking — uses asyncio.sleep() so the event loop
stays responsive.  Pygame init is tracked so we don't re-initialize
every sentence.
"""

import asyncio
import logging
import os
import tempfile
import wave
from typing import Optional

logger = logging.getLogger("COOLEMS.Voice")


class AudioPlayer:
    """Async audio player backed by pygame.mixer."""

    def __init__(self, sample_rate: int = 24000):
        self._sample_rate = sample_rate
        self._playing = False
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._pygame_inited = False
        self._temp_files: list[str] = []

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    async def play_wav_file(self, filepath: str) -> None:
        """Play a WAV file asynchronously — does not block event loop."""
        async with self._lock:
            self._playing = True
            self._stop_event.clear()

        try:
            import pygame

            sample_rate = self._get_sample_rate(filepath)
            await self._ensure_pygame_init_async(sample_rate)
            pygame.mixer.music.load(filepath)
            pygame.mixer.music.play()

            while pygame.mixer.music.get_busy() and not self._stop_event.is_set():
                await asyncio.sleep(0.05)

            if self._stop_event.is_set():
                pygame.mixer.music.stop()

        except Exception as e:
            logger.error("Audio playback failed: %s", e)
        finally:
            async with self._lock:
                self._playing = False

    async def play_wav_bytes(self, wav_bytes: bytes) -> None:
        """
        Play WAV data from bytes asynchronously — does not block event loop.

        Writes bytes to a temp file because pygame.mixer.Sound(BytesIO)
        is unreliable across pygame versions.
        """
        async with self._lock:
            self._playing = True
            self._stop_event.clear()

        temp_path: Optional[str] = None
        try:
            import pygame

            # Write to temp file for reliable pygame loading
            fd, temp_path = tempfile.mkstemp(suffix=".wav")
            with os.fdopen(fd, "wb") as f:
                f.write(wav_bytes)
            self._temp_files.append(temp_path)

            await self._ensure_pygame_init_async(self._sample_rate)
            pygame.mixer.music.load(temp_path)
            pygame.mixer.music.play()

            while pygame.mixer.music.get_busy() and not self._stop_event.is_set():
                await asyncio.sleep(0.05)

            if self._stop_event.is_set():
                pygame.mixer.music.stop()

        except Exception as e:
            logger.error("Audio playback failed: %s", e)
        finally:
            # Clean up temp file
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                    if temp_path in self._temp_files:
                        self._temp_files.remove(temp_path)
                except OSError:
                    logger.debug("Cleanup/delete non-critical error at CLIENT/static/voice/player.py:100")
            async with self._lock:
                self._playing = False

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Stop current playback immediately."""
        async with self._lock:
            self._playing = False

        self._stop_event.set()

        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
                # Do NOT call pygame.mixer.quit() here — it destroys the
                # mixer while the playback loop may still be running,
                # causing crashes.  We keep the mixer alive and only
                # stop the current playback.
        except Exception:
            logger.debug("Non-critical exception caught at CLIENT/static/voice/player.py:124")

    async def is_playing(self) -> bool:
        """Check if audio is currently playing."""
        async with self._lock:
            return self._playing

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_pygame_init(self, sample_rate: int) -> None:
        """Initialize pygame mixer if not already initialized.

        Synchronous — pygame.init() is a blocking C call.
        Call via _ensure_pygame_init_async() from async code.
        """
        if self._pygame_inited:
            return

        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=sample_rate, size=-16, channels=1)
            self._pygame_inited = True

    async def _ensure_pygame_init_async(self, sample_rate: int) -> None:
        """Async wrapper: runs blocking pygame init in a thread executor
        so the event loop stays responsive on first playback."""
        if self._pygame_inited:
            return  # fast path — already initialized, no blocking call

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._ensure_pygame_init, sample_rate
        )

    @staticmethod
    def _get_sample_rate(filepath: str) -> int:
        """Read sample rate from a WAV file header."""
        try:
            with wave.open(filepath, "rb") as wf:
                return wf.getframerate()
        except Exception:
            return 24000
