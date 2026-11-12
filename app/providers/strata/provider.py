"""
StrataProvider — the SERVER talking to a Strata inference endpoint (2026-11).

WHY THIS EXISTS
---------------
config.SERVER_BACKEND chooses the local inference engine behind the COOLEMS SERVER
relay brain: "strata" (Strata - the shipped default since 2026-11) or "llama"
(llama-server, the alternative). Everything the
brain does around generation — WebSocket relay, chat queue, per-slot worker pool, auth
profiles, streaming frames, token stats, stop button — is unchanged; only the HTTP peer
and its lifecycle differ.

Strata speaks the same OpenAI/llama.cpp-compatible surface we already use:
  GET  /health                 {"status":"ok", "max_context":…, "images":bool, "api_key":bool}
  GET  /props                  {"total_slots":…, "model_alias":…, "default_generation_settings":{…}}
  GET  /v1/models              {"data":[{"id":…, "meta":{"n_ctx":…}}, …]}
  POST /v1/chat/completions    SSE stream with delta.content, delta.reasoning_content, usage

so payload building (payload.py), SSE parsing + guards (streaming.py) and the token
statistics stay exactly the llama.cpp code paths. Three real deltas drive this class:

  1. AUTH — /props, /v1/models and /v1/chat/completions require
     "Authorization: Bearer <key>" (/health answers without one). The key comes from
     config/strata_servers.json 'api_key' > config.STRATA_API_KEY > env STRATA_API_KEY;
     every request we make carries the headers built by config.strata_auth_headers().
  2. LIFECYCLE — Strata starts and stops its own inference process. The brain is
     ATTACH-ONLY: start_server() builds the strata registry and probes it; nothing is
     ever spawned, relaunched or terminated (see backend_registry.LlmBackend.spawnable).
  3. MODEL LISTING — there is no local .gguf directory to scan; the models we advertise
     are whatever /v1/models reports, canonical ids only (Strata's alias ids share the
     canonical one's context, and the worker pool queues by canonical model key).

Vision follows Strata's own capability flag: /health.images == false means image input
is refused, so we raise the same typed VisionNotAvailableError the CLIENT already renders
as a "Vision Not Available" card instead of letting llama's mmproj wording leak through.
"""

import asyncio
import logging
import os
import time
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple

from config import (
    HEALTH_CHECK_INTERVAL_SEC,
    PROVIDER_DEFAULT_TIMEOUT,
    strata_auth_headers,
)

from ..llama.provider import LlamaProvider
from ..llama.health import HealthChecker
from ..llama.http_client import get_async_client
from ..llama.streaming import chat_stream as _chat_stream
from ..llama.non_streaming import chat_non_streaming as _chat_non_streaming
from ..llama.server import get_current_model as _get_current_model
from ..llama.model_identity import model_key as model_key_of
from ..llama.vision_support import VisionNotAvailableError

logger = logging.getLogger("COOLEMS.Provider.Strata")

# How long a /health capability answer (images:true/false) is trusted before we re-probe.
# Cheaper than probing on every image-bearing request.
_VISION_PROBE_TTL_SEC = 30.0


class StrataProvider(LlamaProvider):
    """Strata API provider — same wire format as llama.cpp, Bearer-authenticated, attach-only."""

    def __init__(self, api_url: str, timeout: int = PROVIDER_DEFAULT_TIMEOUT,
                 headers: Optional[dict] = None):
        """Args:
            api_url:  Strata base URL (config.STRATA_URL or a strata_servers.json entry).
            timeout:  Generation budget in seconds (same budget the llama path uses).
            headers:  Auth headers for this endpoint. None falls back to
                      config.STRATA_API_KEY, then env STRATA_API_KEY (no key = unauthenticated:
                      /health still works, but /props and chat fail with 401).
        """
        super().__init__(api_url, timeout)
        if headers is None:
            headers = strata_headers_for(api_url)
        self._headers = headers
        # Replace the llama health checker: its /health + /v1/models probes must carry auth,
        # and their failures should name Strata rather than llama-server.
        self._health = HealthChecker(api_url, HEALTH_CHECK_INTERVAL_SEC, headers=self._headers,
                                     label="Strata", start_hint="Strata")
        self._images_enabled: Optional[bool] = None
        self._images_checked_at: float = 0.0

    @property
    def name(self) -> str:
        return "strata"

    @property
    def headers(self) -> Optional[dict]:
        """The auth headers this endpoint is called with (None = no key configured)."""
        return self._headers

    def _strata_error(self, exc: Exception) -> Exception:
        """Re-word a llama.cpp-flavoured failure so it talks about Strata.

        streaming.py / non_streaming.py raise messages written for llama-server ("llama.cpp
        service is not running. Please start llama-server."), and chat_relay forwards the text
        straight to the CLIENT. The wire failure is the same shape under Strata, but the advice
        must point at Strata - so only the engine names are swapped and the HTTP detail the
        backend returned is kept verbatim. Messages that already mention Strata (the 401 branch)
        pass through untouched."""
        text = str(exc)
        if "Strata" in text or "strata" in text:
            return exc
        reworded = text.replace("llama.cpp", "Strata").replace("llama-server", "Strata")
        if isinstance(exc, ConnectionError):
            reworded += (f" Start Strata - the brain attaches to it and never spawns it "
                         f"(GET {self._api_url}/health should answer ok).")
        if isinstance(exc, (ConnectionError, TimeoutError, RuntimeError, FileNotFoundError)):
            return type(exc)(reworded)
        return exc

    # ---- chat_stream ------------------------------------------------------

    async def chat_stream(
        self,
        model: str,
        messages: List[Dict],
        websocket: Any,
        temperature: float = 0.7,
        enable_thinking=None,
        conv_id: str = None,
        tools: list = None,
        stop_event: Optional[asyncio.Event] = None,
        context_window: Optional[int] = None,
    ) -> Tuple[str, Any]:
        """Stream chat completions from Strata (SSE), reusing the llama.cpp stream machinery.

        Adds the Bearer headers, a vision capability check, provider_name="strata" in the
        returned TokenStats, and Strata-worded failures (_strata_error); everything else —
        context guard, model guard, stall watchdog, stop_event, reasoning_content frames,
        real token counts — is the identical code path llama.cpp uses.

        enable_thinking (2026-12) accepts a bool or a canonical thinking level
        "none"/"low"/"medium"/"high"; the level is translated by the serving endpoint's
        ThinkingPolicy (its /props chat_template) into that model's own vocabulary.
        """
        if self._has_image_content(messages) and not await self._strata_images_enabled():
            raise self._vision_error(model)
        try:
            return await _chat_stream(
                api_url=self._api_url,
                timeout=self._timeout,
                model=model,
                messages=messages,
                websocket=websocket,
                temperature=temperature,
                enable_thinking=enable_thinking,
                conv_id=conv_id,
                tools=tools,
                stop_event=stop_event,
                context_window=context_window,
                headers=self._headers,
                provider_name="strata",
            )
        except Exception as exc:  # noqa: BLE001 - re-worded below, never swallowed
            raise self._strata_error(exc) from exc

    # ---- chat_non_streaming -----------------------------------------------

    async def chat_non_streaming(
        self,
        model: str,
        messages: List[Dict],
        temperature: float = 0.7,
        enable_thinking=None,
    ) -> str:
        """Non-streaming chat completion against Strata (same payload, Bearer headers).

        enable_thinking (2026-12) accepts a canonical thinking level string."""
        if self._has_image_content(messages) and not await self._strata_images_enabled():
            raise self._vision_error(model)
        try:
            return await _chat_non_streaming(
                api_url=self._api_url,
                timeout=self._timeout,
                model=model,
                messages=messages,
                temperature=temperature,
                headers=self._headers,
                thinking_mode=enable_thinking if isinstance(enable_thinking, str) else None,
            )
        except Exception as exc:  # noqa: BLE001 - re-worded below, never swallowed
            raise self._strata_error(exc) from exc

    # ---- model listing ----------------------------------------------------

    async def get_models_list(self) -> List[Dict]:
        """Models served by this Strata endpoint — read from /v1/models, no disk scan.

        Strata may expose alias ids alongside the canonical one; aliases route to a model
        group the worker pool has no workers for, so only canonical ids are advertised.
        The catalog merges these with the registry's per-backend groups (slots/health)."""
        models: List[Dict] = []
        seen_keys: set = set()
        try:
            client = await get_async_client()
            resp = await client.get(f"{self._api_url}/v1/models", headers=self._headers)
            if resp.status_code != 200:
                logger.warning("[STRATA] %s/v1/models returned HTTP %s - no strata models listed "
                               "(check the API key)", self._api_url, resp.status_code)
                return models
            for m in resp.json().get("data", []):
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id") or "").strip()
                if not mid or m.get("alias_of"):
                    continue
                key = model_key_of(mid)
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                models.append({
                    "name": mid,
                    "folder": "strata",
                    "size": 0,
                    "on_disk": False,
                    "loaded": False,
                })
        except Exception as e:
            logger.warning("[STRATA] Could not list models from %s: %s", self._api_url, e)
        logger.info("[STRATA] Serving %d model(s): %s", len(models),
                    ", ".join(m["name"] for m in models) or "(none)")
        return models

    async def get_current_model(self, force: bool = False) -> Optional[str]:
        """Currently loaded model id, via /v1/models (Bearer headers) + the per-URL cache."""
        return await _get_current_model(self._api_url, force=force, headers=self._headers)

    # ---- vision capability ------------------------------------------------

    @staticmethod
    def _has_image_content(messages: List[Dict]) -> bool:
        """True if any message carries image content (Ollama 'images' or OpenAI image_url)."""
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("images"):
                return True
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        return False

    async def _strata_images_enabled(self) -> bool:
        """Strata's own capability answer from /health.images (cached, TTL above).

        /health needs no key, so this stays accurate even when chat is unauthenticated."""
        now = time.time()
        if self._images_enabled is not None and (now - self._images_checked_at) < _VISION_PROBE_TTL_SEC:
            return self._images_enabled
        enabled = False
        try:
            client = await get_async_client()
            resp = await client.get(f"{self._api_url}/health", headers=self._headers)
            if resp.status_code == 200:
                enabled = bool(resp.json().get("images", False))
            else:
                logger.debug("[STRATA] /health returned HTTP %s - treating vision as unavailable",
                             resp.status_code)
        except Exception as e:
            logger.debug("[STRATA] /health capability probe failed: %s", e)
        self._images_enabled = enabled
        self._images_checked_at = now
        return enabled

    def _vision_error(self, model: str) -> VisionNotAvailableError:
        """Typed, client-renderable error for image input a Strata endpoint cannot accept.

        Same exception type llama.cpp uses, so chat_relay logs one clean line and the
        CLIENT shows its "Vision Not Available" card without retrying forever."""
        return VisionNotAvailableError(
            "I'm sorry, but vision is not available with the strata backend right now: this "
            "Strata endpoint reports images:false in /health, so it cannot see images. "
            "Send text only, or switch config.SERVER_BACKEND back to \"llama\" with a "
            "vision-capable model (and its mmproj) to work with images.",
            model=model,
        )

    # ---- OCR --------------------------------------------------------------

    async def ocr_transcribe(self, image_base64: str, ocr_models: List[str]) -> str:
        """OCR is a llama.cpp-only feature here: Strata runs no dedicated OCR instance.

        Returns the "ERROR: …" string the OCR handler turns into a failed ocr_response
        frame (raising would only add a traceback for an expected condition)."""
        logger.warning("[STRATA] OCR request refused - the strata backend runs no OCR model "
                       "(asked for: %s)", ", ".join(ocr_models or []) or "none")
        return ("ERROR: OCR is not available with the strata backend. Strata serves one chat "
                "model and has no dedicated OCR instance; switch config.SERVER_BACKEND to "
                "\"llama\" to use the OCR models from profiles.json.")

    # ---- unload/reload ----------------------------------------------------

    def unload_model(self, model: str) -> None:
        """Strata loads and unloads its own model; the brain never touches it."""
        logger.info("[STRATA] unload_model is a no-op - Strata owns its model lifecycle (%s)", model)

    def reload_model(self, model: str) -> None:
        """Strata loads and unloads its own model; the brain never touches it."""
        logger.info("[STRATA] reload_model is a no-op - Strata owns its model lifecycle (%s)", model)

    async def close(self) -> None:
        """Stop the strata registry (poller only — no process is ever killed) and release the pool."""
        from ..llama.backend_registry import get_registry
        reg = get_registry()
        if reg is not None:
            try:
                reg.stop_health_poller()
                await reg.stop_all()   # attach-only backends: nothing is terminated
            except Exception as e:
                logger.warning("[STRATA] Registry shutdown issue (ignored): %s", e)
        from ..llama.http_client import close_async_client, close_sync_session
        await close_async_client()
        close_sync_session()
        logger.info("[STRATA] StrataProvider closed - HTTP pool released (Strata process untouched)")

    # ---- status / error wording -------------------------------------------

    def status_message(self, error: Exception, model_name: str = None, timeout: int = None) -> str:
        """User-friendly Strata wording (llama's status.py says 'llama.cpp' everywhere)."""
        from config import STRATA_URL

        error_str = str(error).lower()
        if "401" in error_str or "credentials" in error_str or "unauthorized" in error_str:
            return (
                "WARNING: Strata Rejected Our Credentials\n\n"
                "Strata requires a Bearer key for /props, /v1/models and chat completion.\n\n"
                "1. Put the key in config/strata_servers.json ('api_key'), or set\n"
                "   STRATA_API_KEY in config/config.py (the env variable works too)\n"
                "2. Check the endpoint is up: visit " + STRATA_URL + "/health\n\n"
                "Then send your message again."
            )
        if "connection" in error_str or "refused" in error_str:
            return (
                "WARNING: Strata Service Not Running\n\n"
                "The AI backend (Strata) is not responding. Please:\n\n"
                "1. Start Strata (its inference endpoint serves " + STRATA_URL + ")\n"
                "2. Check if running: visit " + STRATA_URL + "/health\n\n"
                "The SERVER never starts Strata itself - it only attaches to it.\n"
                "Once Strata is running, send your message again."
            )
        if "timeout" in error_str or "timed out" in error_str:
            time_info = f"({timeout} seconds)" if timeout else ""
            return (
                f"WARNING: Strata Response Timeout\n\n"
                f"The model took too long to respond {time_info}\n\n"
                "1. The request may be near Strata's context limit (--max-context)\n"
                "2. Check Strata's own logs / status page\n"
                "3. Close other workloads on that endpoint"
            )
        if "model" in error_str and ("not found" in error_str or "does not exist" in error_str):
            return (
                f"WARNING: Model Not Found\n\n"
                f"'{model_name}' is not served by this Strata endpoint.\n\n"
                "Check what it serves: visit " + STRATA_URL + "/v1/models (needs the API key),\n"
                "or see the fleet table in the SERVER log."
            )
        return (
            f"WARNING: Strata Error\n\n"
            f"An unexpected error occurred: {str(error)}\n\n"
            "1. Check if Strata is running (" + STRATA_URL + "/health)\n"
            "2. Check its API key in config/strata_servers.json\n"
            "3. Check logs for details"
        )

    # ---- lifecycle --------------------------------------------------------

    async def start_server(self) -> bool:
        """Attach to every configured Strata endpoint — never spawn anything.

        Mirrors LlamaProvider.start_server() but with kind="strata": the registry reads
        config/strata_servers.json, probes /health + /props (Bearer), reports the fleet
        table and keeps a health poller running. The brain does not launch, relaunch or
        terminate Strata — its process belongs to Strata."""
        from ..llama.backend_registry import LlmBackendRegistry, set_registry

        reg = LlmBackendRegistry.from_config(kind="strata")
        set_registry(reg)
        ok = await reg.start_all()
        if ok:
            reg.start_health_poller(interval_sec=HEALTH_CHECK_INTERVAL_SEC)
        else:
            logger.error(
                "[STRATA] No Strata endpoint is healthy - start Strata locally and check the "
                "API key in config/strata_servers.json; chat stays refused until one answers "
                "/health AND /props"
            )
        return ok

    # ---- unload/reload ----------------------------------------------------
    # Inherited no-ops: Strata loads its model itself, and llama's node launcher is not
    # used at all in this mode.


def _endpoint_host_port(url: str) -> Optional[Tuple[str, Optional[int]]]:
    """(host, port) of an endpoint URL, with loopback spellings unified (2026-11).

    config.STRATA_URL is spelled 'localhost' while a strata_servers.json entry says
    '127.0.0.1' - the same endpoint, so key lookup must not read them as two hosts."""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return None
    if "://" not in raw:
        raw = "//" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "::1"):
        host = "127.0.0.1"
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        port = None
    return (host, port)


def strata_headers_for(api_url: str) -> Optional[dict]:
    """Auth headers for one Strata endpoint (2026-11).

    Key precedence, mirroring config.get_strata_server_instances(): the 'api_key' of the
    config/strata_servers.json entry whose host:port matches *api_url*, then
    config.STRATA_API_KEY, then env STRATA_API_KEY. Returns None when no key is set —
    /health still works, /props and chat answer 401."""
    wanted = _endpoint_host_port(api_url)
    try:
        import config
        key = ""
        for inst in config.get_strata_server_instances():
            if _endpoint_host_port(inst.api_url) == wanted:
                if inst.api_key:
                    return strata_auth_headers(inst.api_key)
                break
        key = (getattr(config, "STRATA_API_KEY", "") or "").strip()
    except Exception as e:  # pragma: no cover - defensive: a broken config never blocks chat
        logger.debug("[STRATA] key lookup for %s failed: %s", api_url, e)
        key = ""
    if not key:
        key = (os.environ.get("STRATA_API_KEY") or "").strip()
    return strata_auth_headers(key)
