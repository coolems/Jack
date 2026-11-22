"""addons.json loading + validation (2026-07): the Settings > Addons tab contract.

CLIENT/client_config/addons.json is the single source of truth for the voice addons
(TTS + STT). Every entry passes the SAME validator at startup and on every settings
save; a bad entry is reported, never written. Nothing is enabled by default.

Shape (per addon):
  tts: name, enabled, provider (supertonic|openai), model, speed, total_steps,
       silence_duration, detect_language, fallback_lang, voices {en,de,ro},
       speaker (audio output deviceId; "" = system default),
       openai_model, openai_voice, openai_api_key
  stt: name, enabled, provider (faster_whisper|openai), model, device, language,
       mic (input deviceId; "" = system default), openai_model, openai_api_key
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("COOLEMS")

#: <CLIENT> root anchored to this file (app/addons -> 2 up), never CWD-relative.
_CLIENT_ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".."))
ADDONS_FILE = os.path.join(_CLIENT_ROOT, "client_config", "addons.json")

#: Models the local engines accept (kept small on purpose - the tab offers a picker,
#: not a free-text box, so a typo can never silently break synthesis).
TTS_MODELS = ("supertonic-2", "supertonic-3")
TTS_PROVIDERS = ("supertonic", "openai")
STT_PROVIDERS = ("faster_whisper", "openai")
STT_MODELS = ("tiny", "base", "small", "medium", "large-v3")
STT_DEVICES = ("auto", "cpu", "cuda")
LANGS = ("en", "de", "ro")
VOICES = ("M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5")
OPENAI_VOICES = ("alloy", "echo", "fable", "onyx", "nova", "shimmer", "coral",
                 "verse", "ballad", "ash", "sage", "amuch", "shield")


@dataclass
class TtsConfig:
    """One TTS addon entry (validated)."""
    name: str = "tts"
    enabled: bool = False
    provider: str = "supertonic"          # supertonic (local) | openai
    model: str = "supertonic-3"           # local model
    speed: float = 1.0                    # 0.5 - 2.0
    total_steps: int = 10                 # 1 - 100 (synthesis quality)
    silence_duration: float = 0.3         # 0.0 - 2.0 s between chunks
    detect_language: bool = True          # auto-detect en/de/ro from the text
    fallback_lang: str = "en"             # used when detection finds no fingerprint
    voices: Dict[str, str] = field(default_factory=lambda: {"en": "M1", "de": "M1", "ro": "M1"})
    speaker: str = ""                     # audio output deviceId; "" = system default
    openai_model: str = "gpt-4o-mini-tts"
    openai_voice: str = "alloy"
    openai_api_key: str = ""


@dataclass
class SttConfig:
    """One STT addon entry (validated)."""
    name: str = "stt"
    enabled: bool = False
    provider: str = "faster_whisper"      # faster_whisper (local) | openai
    model: str = "small"                  # tiny|base|small|medium|large-v3
    device: str = "auto"                  # auto|cpu|cuda
    language: str = "auto"                # auto|en|de|ro
    mic: str = ""                         # input deviceId; "" = system default
    openai_model: str = "gpt-4o-mini-transcribe"
    openai_api_key: str = ""


def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, f))


def _int_clamp(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        return min(hi, max(lo, int(v)))
    except (TypeError, ValueError):
        return default


def _entry(raw: Any, problems: List[str]) -> Optional[Any]:
    """Validate one raw entry -> TtsConfig/SttConfig, or None (problem appended)."""
    if not isinstance(raw, dict):
        problems.append("entry: not an object")
        return None
    name = str(raw.get("name", "")).strip()
    if name not in ("tts", "stt"):
        problems.append(f"entry '{name}': name must be 'tts' or 'stt'")
        return None

    if name == "tts":
        provider = str(raw.get("provider", "supertonic"))
        if provider not in TTS_PROVIDERS:
            problems.append(f"tts: provider must be one of {TTS_PROVIDERS}")
            return None
        voices_raw = raw.get("voices") or {}
        voices: Dict[str, str] = {}
        for lang in LANGS:
            v = str(voices_raw.get(lang, "M1")) if isinstance(voices_raw, dict) else "M1"
            if v not in VOICES:
                problems.append(f"tts: voices.{lang} must be one of {VOICES}")
                return None
            voices[lang] = v
        fb = str(raw.get("fallback_lang", "en"))
        if fb not in LANGS:
            problems.append(f"tts: fallback_lang must be one of {LANGS}")
            return None
        model = str(raw.get("model", "supertonic-3"))
        if model not in TTS_MODELS:
            problems.append(f"tts: model must be one of {TTS_MODELS}")
            return None
        ov = str(raw.get("openai_voice", "alloy"))
        if ov not in OPENAI_VOICES:
            problems.append(f"tts: openai_voice must be one of {OPENAI_VOICES}")
            return None
        return TtsConfig(
            name=name,
            enabled=bool(raw.get("enabled", False)),
            provider=provider,
            model=model,
            speed=_clamp(raw.get("speed", 1.0), 0.5, 2.0, 1.0),
            total_steps=_int_clamp(raw.get("total_steps", 10), 1, 100, 10),
            silence_duration=_clamp(raw.get("silence_duration", 0.3), 0.0, 2.0, 0.3),
            detect_language=bool(raw.get("detect_language", True)),
            fallback_lang=fb,
            voices=voices,
            speaker=str(raw.get("speaker", ""))[:128],
            openai_model=str(raw.get("openai_model", "gpt-4o-mini-tts")),
            openai_voice=ov,
            openai_api_key=str(raw.get("openai_api_key", "")),
        )

    # stt
    provider = str(raw.get("provider", "faster_whisper"))
    if provider not in STT_PROVIDERS:
        problems.append(f"stt: provider must be one of {STT_PROVIDERS}")
        return None
    model = str(raw.get("model", "small"))
    if model not in STT_MODELS:
        problems.append(f"stt: model must be one of {STT_MODELS}")
        return None
    device = str(raw.get("device", "auto"))
    if device not in STT_DEVICES:
        problems.append(f"stt: device must be one of {STT_DEVICES}")
        return None
    language = str(raw.get("language", "auto"))
    if language not in ("auto",) + LANGS:
        problems.append(f"stt: language must be 'auto' or one of {LANGS}")
        return None
    return SttConfig(
        name=name,
        enabled=bool(raw.get("enabled", False)),
        provider=provider,
        model=model,
        device=device,
        language=language,
        mic=str(raw.get("mic", ""))[:128],
        openai_model=str(raw.get("openai_model", "gpt-4o-mini-transcribe")),
        openai_api_key=str(raw.get("openai_api_key", "")),
    )


def load_addons() -> tuple:
    """Read addons.json -> (entries, problems). Missing file = defaults, no problem."""
    problems: List[str] = []
    entries: List[Any] = []
    try:
        with open(ADDONS_FILE, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        doc = {}
    except (json.JSONDecodeError, OSError) as e:
        problems.append(f"addons.json unreadable ({e}) - using defaults")
        doc = {}

    raw_list = doc.get("addons") if isinstance(doc, dict) else None
    if not isinstance(raw_list, list):
        raw_list = []
    seen = set()
    for raw in raw_list:
        cfg = _entry(raw, problems)
        if cfg is None:
            continue
        if cfg.name in seen:
            problems.append(f"{cfg.name}: duplicate entry (one per addon)")
            continue
        seen.add(cfg.name)
        entries.append(cfg)

    # Guarantee both addons exist (missing ones come back disabled with defaults).
    have = {e.name for e in entries}
    if "tts" not in have:
        entries.append(TtsConfig())
    if "stt" not in have:
        entries.append(SttConfig())
    return entries, problems


def save_addons(entries: List[Any]) -> None:
    """Write the validated list back (atomic replace, LF endings, comments kept)."""
    doc: Dict[str, Any] = {}
    if os.path.exists(ADDONS_FILE):
        try:
            with open(ADDONS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                doc = {k: v for k, v in existing.items() if k != "addons"}
        except (json.JSONDecodeError, OSError):
            doc = {}
    if not doc.get("_comment"):
        doc["_comment"] = [
            "Voice addons the CLIENT can use: TTS (speak text) and STT (transcribe audio).",
            "Managed live from Settings > Addons (POST /api/addons); you can also edit this",
            "file directly - it is re-read on every save and at startup.",
            "NOTHING IS ENABLED BY DEFAULT. Local providers are free and private (audio",
            "never leaves this machine); the OpenAI providers need an API key and send",
            "audio to OpenAI.",
        ]
    doc["addons"] = [_entry_dict(e) for e in entries]
    tmp = ADDONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, ADDONS_FILE)


def _entry_dict(e: Any) -> Dict[str, Any]:
    """Config dataclass -> the JSON shape addons.json stores (stable key order)."""
    if isinstance(e, TtsConfig):
        return {
            "name": e.name, "enabled": bool(e.enabled), "provider": e.provider,
            "model": e.model, "speed": e.speed, "total_steps": e.total_steps,
            "silence_duration": e.silence_duration, "detect_language": e.detect_language,
            "fallback_lang": e.fallback_lang, "voices": dict(e.voices),
            "speaker": e.speaker,
            "openai_model": e.openai_model, "openai_voice": e.openai_voice,
            "openai_api_key": e.openai_api_key,
        }
    return {
        "name": e.name, "enabled": bool(e.enabled), "provider": e.provider,
        "model": e.model, "device": e.device, "language": e.language,
        "mic": e.mic,
        "openai_model": e.openai_model, "openai_api_key": e.openai_api_key,
    }
