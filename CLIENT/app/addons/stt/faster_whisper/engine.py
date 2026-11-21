"""faster-whisper local STT engine (2026-07).

One model auto-detects EN/DE/RO (or any of Whisper's 100+ languages) - zero config.
The model downloads to .temp/addons/models/ on first load (~1.5 GB for 'medium').

Surface (used by app/addons/stt/__init__.make_engine):
  await load()
  await transcribe(audio_bytes, fmt="webm") -> text
  close()
"""

import asyncio
import io
import logging
from typing import Any, Optional

logger = logging.getLogger("COOLEMS")


class FasterWhisperEngine:
    """Thin async wrapper around faster_whisper.WhisperModel."""

    def __init__(self, cfg: Any):
        self._cfg = cfg
        self._model: Optional[Any] = None
        self._lock = asyncio.Lock()

    # -- model lifecycle ------------------------------------------------

    async def load(self) -> None:
        if self._model is not None:
            return

        def _load():
            from faster_whisper import WhisperModel
            device = self._cfg.device
            compute = "default"
            if device == "auto":
                try:
                    import ctranslate2
                    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                except Exception:
                    device = "cpu"
            model = WhisperModel(self._cfg.model, device=device, compute_type=compute)
            logger.info("[STT] faster-whisper %s loaded on %s", self._cfg.model, device)
            return model

        self._model = await asyncio.get_running_loop().run_in_executor(None, _load)

    def close(self) -> None:
        self._model = None

    # -- transcription ----------------------------------------------------

    def _transcribe(self, audio_bytes: bytes, fmt: str) -> str:
        """Blocking: decode audio, run Whisper. Runs in an executor."""
        import numpy as np

        def _decode() -> np.ndarray:
            """Audio bytes -> float32 mono 16 kHz (PyAV, faster-whisper's input)."""
            import av
            container = av.open(io.BytesIO(audio_bytes))
            resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
            frames = []
            for frame in container.decode(audio=0):
                for rf in resampler.resample(frame):
                    arr = rf.to_ndarray()
                    if arr.size:
                        frames.append(arr[0])
            container.close()
            if not frames:
                raise ValueError("no audio frames found in the upload")
            return np.concatenate(frames)

        audio = _decode()
        language = None if self._cfg.language == "auto" else self._cfg.language
        segments, _info = self._model.transcribe(
            audio, language=language, vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
        logger.info("[STT] faster-whisper %s: %d bytes -> %d chars (lang=%s)",
                    self._cfg.model, len(audio_bytes), len(text),
                    _info.language if _info else "?")
        return text

    async def transcribe(self, audio_bytes: bytes, fmt: str = "webm") -> str:
        if not audio_bytes:
            raise ValueError("empty audio upload")
        if self._model is None:
            await self.load()
        async with self._lock:  # one transcription at a time (model is not thread-safe)
            return await asyncio.get_running_loop().run_in_executor(
                None, self._transcribe, audio_bytes, fmt)
