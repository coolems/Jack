"""Voice addon endpoints (2026-07): the Settings > Addons tab + the two services.

The tab reads/writes CLIENT/client_config/addons.json (MCP pattern):
  GET  /api/addons            -> every addon (enabled AND disabled) + live engine state
  POST /api/addons            -> full replacement of the "addons" list; validated with
                                 the startup validator, written atomically, re-wired
                                 live (no restart). On ANY problem nothing is written.

The services (used by the UI mic button / playback, later by model tools):
  POST /api/addons/transcribe -> multipart audio upload -> {"text": "..."}
  GET  /api/addons/speech     -> ?text=...&voice=...&lang=... -> audio bytes
                                 (lang='' auto-detects en/de/ro from the text)

Security model: enabling an addon is a HUMAN decision made in this UI. Local
providers never touch the network; the OpenAI providers send audio to OpenAI and
need a key stored in addons.json - the tab shows when a key is missing.
"""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, UploadFile
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger("COOLEMS")

#: Upload cap for the mic button (a few minutes of webm) - bigger goes to the file panel.
_MAX_AUDIO_BYTES = 20 * 1024 * 1024


def _row(cfg: Any, live: Dict[str, Any]) -> Dict[str, Any]:
    """Config + live engine state -> one tab row (stable key order)."""
    from app.addons.config import _entry_dict
    row = _entry_dict(cfg)
    row["state"] = live.get("state", "disabled")
    if live.get("error"):
        row["error"] = live["error"]
    return row


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:

    @router.get("/api/addons")
    async def get_addons():
        """All configured addons + live engine state. For the Settings > Addons tab."""
        from app.addons.config import ADDONS_FILE, load_addons
        from app.addons.registry import get_registry

        entries, problems = load_addons()
        reg = get_registry()
        live = {r["name"]: r for r in (reg.status() if reg is not None else [])}
        return {
            "addons": [_row(e, live.get(e.name, {})) for e in entries],
            "problems": problems,
            "config_file": ADDONS_FILE,
        }

    @router.post("/api/addons")
    async def save_addons(data: Dict):
        """Replace the whole addons list and re-wire live.

        Body: {"addons": [ {name, enabled, provider, ...}, ... ]} - every entry is
        validated with the startup validator; on ANY problem nothing is written."""
        import json
        import os

        from app.addons.config import ADDONS_FILE, _entry, load_addons, save_addons
        from app.addons.registry import rewire

        raw_addons = data.get("addons") if isinstance(data, dict) else None
        if not isinstance(raw_addons, list):
            return JSONResponse(status_code=400, content={"detail": 'body must be {"addons": [...]}'})

        problems: List[str] = []
        parsed: List[Any] = []
        seen = set()
        for i, raw in enumerate(raw_addons, 1):
            before = len(problems)
            cfg = _entry(raw, problems)
            if cfg is None:
                if len(problems) == before:
                    problems.append(f"entry #{i}: not an object")
                continue
            if cfg.name in seen:
                problems.append(f"{cfg.name}: duplicate entry (one per addon)")
                continue
            seen.add(cfg.name)
            parsed.append(cfg)

        if problems:
            return JSONResponse(status_code=400, content={"detail": "; ".join(problems)})

        try:
            save_addons(parsed)
        except (OSError, ValueError, TypeError) as e:
            logger.error(f"[Addons] failed to save addons.json: {e}")
            return JSONResponse(status_code=500,
                                content={"detail": f"Failed to write client_config/addons.json: {e}"})

        await rewire()

        # Fresh read-back so the UI shows exactly what is on disk + live state now.
        from app.addons.registry import get_registry
        entries, problems2 = load_addons()
        reg = get_registry()
        live = {r["name"]: r for r in (reg.status() if reg is not None else [])}
        enabled = sorted(e.name for e in entries if e.enabled)
        logger.info(f"[Addons] settings save: {len(parsed)} addon(s), enabled={enabled or 'none'}")
        return {
            "status": "ok",
            "addons": [_row(e, live.get(e.name, {})) for e in entries],
            "problems": problems2,
            "config_file": ADDONS_FILE,
        }

    @router.post("/api/addons/transcribe")
    async def transcribe_audio(file: UploadFile):
        """Audio upload (webm/wav/mp3/ogg/m4a) -> {"text": "..."} via the active STT."""
        from app.addons.registry import get_registry

        reg = get_registry()
        if reg is None:
            return JSONResponse(status_code=400,
                                content={"detail": "STT addon is disabled - enable it in Settings > Addons"})
        audio = await file.read()
        if not audio:
            return JSONResponse(status_code=400, content={"detail": "empty audio upload"})
        if len(audio) > _MAX_AUDIO_BYTES:
            return JSONResponse(status_code=413,
                                content={"detail": f"audio too large (max {_MAX_AUDIO_BYTES // (1024 * 1024)} MB)"})
        fmt = (file.filename or "webm").rsplit(".", 1)[-1].lower() if file.filename else "webm"
        try:
            text = await reg.transcribe(audio, fmt=fmt)
        except Exception as e:
            logger.warning(f"[Addons] transcribe failed: {e}")
            return JSONResponse(status_code=500, content={"detail": str(e)})
        return {"text": text}

    @router.get("/api/addons/speech")
    async def speech(text: str = "", voice: str = "", lang: str = ""):
        """Text -> audio bytes via the active TTS.

        lang: '' = auto-detect en/de/ro from the text (when detect_language is on),
        or 'en'/'de'/'ro' to force. voice: '' = the per-language voice from config."""
        from app.addons.registry import get_registry

        if not text.strip():
            return JSONResponse(status_code=400, content={"detail": "no text to speak"})
        if len(text) > 20000:
            return JSONResponse(status_code=413, content={"detail": "text too long (max 20000 chars)"})
        reg = get_registry()
        if reg is None:
            return JSONResponse(status_code=400,
                                content={"detail": "TTS addon is disabled - enable it in Settings > Addons"})
        try:
            audio = await reg.speak(text, voice=voice, lang=lang)
        except Exception as e:
            logger.warning(f"[Addons] speech failed: {e}")
            return JSONResponse(status_code=500, content={"detail": str(e)})
        # Local Supertonic returns WAV; OpenAI returns MP3 - sniff the header.
        ctype = "audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg"
        return Response(content=audio, media_type=ctype)
