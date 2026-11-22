"""Supertonic local TTS engine (2026-07).

One model speaks EN/DE/RO. The model loads once per process (lazy, in an executor -
the first load downloads the model to .temp/addons/models/ and takes a few seconds).
speak() returns one WAV per call; long texts are chunked per sentence so the first
bytes arrive fast.

Surface (used by app/addons/tts/__init__.make_engine):
  await load()
  await speak(text, voice="", lang="") -> WAV bytes
  close()
"""

import asyncio
import io
import logging
import os
from typing import Any, Optional

from app.addons.tts.supertonic.text_utils import (
    detect_language,
    sanitize_for_lang,
    split_into_sentences,
)

logger = logging.getLogger("COOLEMS")


class SupertonicEngine:
    """Thin async wrapper around the supertonic TTS model."""

    def __init__(self, cfg: Any):
        self._cfg = cfg
        self._model = None
        self._lock = asyncio.Lock()

    # -- model lifecycle ------------------------------------------------

    async def load(self) -> None:
        """Load the model (downloads it on first run). Blocks in an executor."""
        if self._model is not None:
            return

        def _load():
            from supertonic import TTS
            model = TTS(model=self._cfg.model)
            logger.info(
                "[TTS] supertonic %s loaded - voices: %s, sr: %s, multilingual: %s",
                self._cfg.model, model.voice_style_names, model.sample_rate,
                model.is_multilingual,
            )
            return model

        self._model = await asyncio.get_running_loop().run_in_executor(None, _load)

    def close(self) -> None:
        self._model = None

    # -- synthesis --------------------------------------------------------

    def _pick_voice(self, voice: str, lang: str) -> str:
        """Explicit voice wins; otherwise the per-language voice from addons.json."""
        if voice:
            return voice
        return self._cfg.voices.get(lang, self._cfg.voices.get(self._cfg.fallback_lang, "M1"))

    def _synth_sentence(self, text: str, voice: str, lang: str):
        """Blocking: one sentence -> (waveform, duration). Runs in an executor."""
        model = self._model
        style = model.get_voice_style(voice)
        return model.synthesize(
            text=text,
            voice_style=style,
            total_steps=self._cfg.total_steps,
            speed=self._cfg.speed,
            silence_duration=self._cfg.silence_duration,
            lang=lang,
        )

    def _to_wav(self, waveform, sample_rate: int) -> bytes:
        """Waveform tensor -> WAV bytes (soundfile, stdlib wave fallback)."""
        buffer = io.BytesIO()
        try:
            import soundfile as sf
            sf.write(buffer, waveform.squeeze(), sample_rate, format="WAV")
        except ImportError:
            import wave as wave_mod
            with wave_mod.open(buffer, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                data = (waveform.squeeze() * 32767).astype("int16")
                wf.writeframes(data.tobytes())
        return buffer.getvalue()

    async def speak(self, text: str, voice: str = "", lang: str = "") -> bytes:
        """Text -> WAV bytes. lang='' auto-detects en/de/ro from the text."""
        if not text or not text.strip():
            raise ValueError("no text to speak")
        if self._model is None:
            await self.load()

        lang = lang or (
            detect_language(text, self._cfg.fallback_lang)
            if self._cfg.detect_language else self._cfg.fallback_lang
        )
        voice = self._pick_voice(voice, lang)
        sentences = [
            sanitize_for_lang(s, lang, self._cfg.model)
            for s in split_into_sentences(text)
        ]
        sentences = [s for s in sentences if s]
        if not sentences:
            raise ValueError("text became empty after sanitization")

        model = self._model
        loop = asyncio.get_running_loop()
        wav_chunks = []
        async with self._lock:  # one synthesis at a time (the model is not thread-safe)
            for i, sentence in enumerate(sentences):
                waveform, _dur = await loop.run_in_executor(
                    None, self._synth_sentence, sentence, voice, lang)
                wav_chunks.append(self._to_wav(waveform, model.sample_rate))
                logger.debug("[TTS] %s/%d sentence(s) done (lang=%s voice=%s)",
                             i + 1, len(sentences), lang, voice)

        return _concat_wav(wav_chunks)


def _concat_wav(chunks: list) -> bytes:
    """Join WAV chunks (same sample rate) into one WAV.

    One chunk = the whole text is already a single WAV (just return it).
    """
    if len(chunks) == 1:
        return chunks[0]
    import wave as wave_mod
    frames = b""
    rate = None
    for chunk in chunks:
        with wave_mod.open(io.BytesIO(chunk), "rb") as wf:
            if rate is None:
                rate = wf.getframerate()
            frames += wf.readframes(wf.getnframes())
    out = io.BytesIO()
    with wave_mod.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)
    return out.getvalue()
