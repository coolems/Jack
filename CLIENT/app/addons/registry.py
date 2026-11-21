"""Addons registry (2026-07): eager preload on enable + live status.

One loaded engine per enabled addon. A disabled addon costs exactly zero (no import,
no model download). Enabling via the settings save re-wires here: the old engine is
released and the new one starts loading IMMEDIATELY in a background task (not at
first use), so a save never blocks on a model download (first Whisper 'medium' load
downloads ~1.5 GB) and the first real use is instant.

Public surface:
  get_registry() / set_registry()  - process-wide singleton (MCP pattern)
  rewire()                         - rebuild from addons.json after a settings save
  status()                         - per-addon live state for GET /api/addons
  speak(text, voice, lang)         - TTS bytes (routes to the active provider)
  transcribe(audio_bytes, fmt)     - STT text (routes to the active provider)
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("COOLEMS")

#: <CLIENT>/.temp/addons - models auto-download here (gitignored by the .temp rule).
_MODELS_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".temp", "addons", "models"))

_registry: Optional["AddonsRegistry"] = None


def get_registry() -> Optional["AddonsRegistry"]:
    return _registry


def set_registry(reg: Optional["AddonsRegistry"]) -> None:
    global _registry
    _registry = reg


def wire_addons() -> Optional["AddonsRegistry"]:
    """Build a registry from addons.json; None when nothing is enabled.

    Always registers the result process-wide (even None) so the endpoints see the
    current state without a separate set_registry call."""
    from app.addons.config import load_addons
    entries, _problems = load_addons()
    enabled = [e for e in entries if e.enabled]
    reg = AddonsRegistry(entries) if enabled else None
    set_registry(reg)
    return reg


class AddonsRegistry:
    """Holds the live engines for the enabled addons."""

    def __init__(self, entries: List[Any]):
        self._entries = {e.name: e for e in entries}
        self._engines: Dict[str, Any] = {}
        self._errors: Dict[str, str] = {}
        self._loading: Dict[str, bool] = {}
        self._info: Dict[str, str] = {}
        self._tasks: set = set()

    # -- config access ------------------------------------------------

    def entry(self, name: str) -> Optional[Any]:
        return self._entries.get(name)

    def entries(self) -> List[Any]:
        return list(self._entries.values())

    # -- engine loading (preloaded on enable, never at first use) -------

    def _load_task(self, name: str) -> None:
        """Start the background load for one addon (kept referenced, never GC'd).

        Disabled addons are never loaded: a preload loop (startup / rewire) may
        visit every entry, but only enabled ones may download their model."""
        cfg = self._entries.get(name)
        if cfg is None or not cfg.enabled:
            return
        if name in self._engines or self._loading.get(name):
            return
        self._loading[name] = True

        async def _load():
            try:
                if name == "tts":
                    from app.addons.tts import make_engine
                else:
                    from app.addons.stt import make_engine
                self._engines[name] = await self._load_with_deps(name, make_engine)
                self._errors.pop(name, None)
                self._info.pop(name, None)
                logger.info(f"[Addons] {name} loaded ({self._entries[name].provider})")
            except Exception as e:  # pragma: no cover - engine load failure is data
                self._errors[name] = str(e)
                self._info.pop(name, None)
                logger.warning(f"[Addons] {name} failed to load: {e}")
            finally:
                self._loading[name] = False

        task = asyncio.create_task(_load(), name=f"addon-load-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _load_with_deps(self, name: str, make_engine: Any) -> Any:
        """Load the engine; on a missing package, auto-install it and retry once.

        Enabling an addon must just work: the first load pip-installs the pinned
        packages (app/addons/_deps.py) into the running venv, then retries."""
        provider = self._entries[name].provider
        try:
            return await make_engine(self._entries[name])
        except (ImportError, ModuleNotFoundError):
            from app.addons._deps import deps_for, install
            specs = deps_for(provider)
            if not specs:
                raise
            self._info[name] = f"Installing {' '.join(specs)} (first time, one-off)..."
            logger.info(f"[Addons] {name}: package missing - auto-installing {specs}")
            await asyncio.get_running_loop().run_in_executor(None, install, specs)
            self._info.pop(name, None)
            return await make_engine(self._entries[name])

    async def engine(self, name: str) -> Optional[Any]:
        """Return the loaded engine, awaiting the in-flight preload if still running."""
        cfg = self._entries.get(name)
        if cfg is None or not cfg.enabled:
            return None
        if name in self._engines:
            return self._engines[name]
        self._load_task(name)
        # Wait for the in-flight load (bounded by the model download on first run).
        for _ in range(600):
            if name in self._engines or not self._loading.get(name):
                break
            await asyncio.sleep(0.5)
        return self._engines.get(name)

    def close(self) -> None:
        """Release engines (called before a re-wire)."""
        for name in list(self._engines):
            try:
                self._engines[name].close()
            except Exception:  # pragma: no cover - teardown must never raise
                pass
            self._engines.pop(name, None)
        self._errors.clear()
        self._info.clear()

    # -- live status for GET /api/addons -------------------------------

    def status(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for name, cfg in self._entries.items():
            row: Dict[str, Any] = {"name": name, "enabled": bool(cfg.enabled)}
            if not cfg.enabled:
                row["state"] = "disabled"
            elif name in self._engines:
                row["state"] = "loaded"
                row["provider"] = cfg.provider
            elif self._loading.get(name):
                row["state"] = "loading"
                row["provider"] = cfg.provider
                if self._info.get(name):
                    row["info"] = self._info[name]
            elif name in self._errors:
                row["state"] = "error"
                row["error"] = self._errors[name]
            else:
                # Enabled but no load task running (e.g. registry rebuilt after a
                # rewire that has not started the preload yet) - the first request
                # will start it; the UI shows the generic "loading" line.
                row["state"] = "loading"
                row["provider"] = cfg.provider
            rows.append(row)
        return rows

    # -- the two service calls ------------------------------------------

    async def speak(self, text: str, voice: str = "", lang: str = "") -> bytes:
        """TTS: text -> WAV bytes. voice/lang override the config when given."""
        eng = await self.engine("tts")
        if eng is None:
            raise RuntimeError("TTS addon is not enabled (Settings > Addons)")
        return await eng.speak(text, voice=voice, lang=lang)

    async def transcribe(self, audio_bytes: bytes, fmt: str = "webm") -> str:
        """STT: audio bytes -> transcript text."""
        eng = await self.engine("stt")
        if eng is None:
            raise RuntimeError("STT addon is not enabled (Settings > Addons)")
        return await eng.transcribe(audio_bytes, fmt=fmt)


async def rewire() -> None:
    """Close the current registry and rebuild it from addons.json (settings save)."""
    old = get_registry()
    if old is not None:
        old.close()
    try:
        reg = wire_addons()
    except Exception as e:  # pragma: no cover - a save must never 500
        logger.warning(f"[Addons] re-wire failed (non-fatal): {e}")
        reg = None
    set_registry(reg)
    if reg is not None:
        enabled = [e.name for e in reg.entries() if e.enabled]
        logger.info(f"[Addons] re-wired - enabled: {enabled}")
        # Pre-load the engines in the background so the first real use is instant.
        for e in reg.entries():
            reg._load_task(e.name)
