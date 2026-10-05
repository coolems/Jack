"""
Agent/Model endpoints - Models, provider status, agent DNA, learning, stop generation.
UPDATED: Role-based model filtering happens ON THE SERVER (pre-filtered lists).
UPDATED: Model switching endpoint for llama.cpp
UPDATED: Settings endpoint for server_address

SECURITY BOUNDARY (2026-07): The CLIENT no longer resolves permissions from
profiles.json or scans disk. The SERVER authenticates the user's key over WS
and only sends model lists / tool definitions allowed for that role.

MODEL SWITCH ERROR HANDLING (2026-08-20):
  - While a switch is in progress, "SERVER not answering" is an EXPECTED state:
    it is logged as INFO/WARNING with one clean line - never as an ERROR traceback.
  - The background switch task waits for the SERVER reload within the configured
    budget (config.MODEL_SWITCH_*). No fallbacks: we either confirm the new model
    loaded or report a single, clear failure.

SETUP MODE (2026-09-01): POST /api/auth/set-key writes the first API key while no
key is configured yet. The auth middleware lets ONLY this path through in setup
mode (and only from loopback) - it is the one action possible before a key exists.
"""

from typing import Dict
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# (2026-08-20 review #7): live reader instead of an import-time capture -- the SERVER
# rewrites CONTEXT_WINDOW_TOKENS after every auth via apply_auth_info().
import config.config as _cfg_live
import logging
import os
import asyncio
from app.providers.coolems.client_provider import is_model_switching, _set_model_switching

logger = logging.getLogger("COOLEMS")

# Module-level list to keep references to background model switch tasks.
_model_switch_tasks: list = []


def create_agent_router(provider, model_name: str, api_timeout: int,
                        agent, stop_events) -> APIRouter:
    """Factory function that returns a configured agent/models router."""
    router = APIRouter(tags=["agent"])

    # ===== SETUP MODE ENDPOINT (2026-09-01) =====
    @router.post("/api/auth/set-key")
    async def set_api_key(request: Request, data: Dict):
        """Record API-key bookkeeping ({email, date_acquired}) in CLIENT/config/.api_client_keys.json.

        This is the ONLY endpoint that works while no real key exists yet (setup mode)
        - APIMiddleware routes it through for loopback callers and rejects everything
        else with an actionable 401 until this succeeds. In keyed mode it stays
        available so the owner can change the key from Settings; the NEW key is still
        validated by the SERVER on connect, so nothing untrusted ever gets access.

        SECURITY (2026-09-29): the key itself is NEVER written to disk - it lives in the
        browser (localStorage) and in the SERVER's config/.api_keys.json only. The file
        records email + date_acquired; *email* comes from the X-User-Email header when
        present, otherwise an optional "email" body field.

        Body: {"key": "<api key>", "email": "<optional>"}
        """
        try:
            import app.keys as _keys_mod

            email = ""
            headers = dict(request.headers)
            email_header = (headers.get(b"x-user-email") or b"").decode("utf-8", errors="replace").strip()
            if email_header:
                email = email_header
            else:
                email = str(data.get("email") or "").strip()

            result = _keys_mod.set_client_api_key(str(data.get("key", "") or ""), email)
            if not result.get("ok"):
                return JSONResponse(status_code=400, content={"detail": result.get("error")})
            logger.info(f"API key saved via /api/auth/set-key: {result['message']}")
            return {"status": "ok", "message": result["message"]}
        except Exception as e:
            logger.error(f"Failed to save API key: {e}")
            return JSONResponse(
                status_code=500,
                content={"detail": f"Failed to save API key: {str(e)}"}
            )

    # ===== MEDIA TOKEN ENDPOINT (SECURITY 2026-10-05) =====
    @router.post("/api/auth/media-token")
    async def issue_media_token(data: Dict):
        """Mint a short-lived, single-use media token for ONE exact file path.

        Reached only AFTER the APIMiddleware passed header auth (X-API-Key + X-User-Email),
        so it grants nothing beyond what the caller already proved. The returned ?t= token is
        bound to that exact path, expires in 60 s and allows at most 3 uses - a leaked URL is
        useless afterwards (replaces the old ?api_key=...&email=... sub-resource pattern).

        Body: {"path": "<working_root-relative file path>"}
        Returns: {"token": "...", "expires_in": 60}
        """
        try:
            from app.media_tokens import issue_token as _issue_media_token, TTL_S

            rel_path = str((data or {}).get("path") or "").strip()
            if not rel_path:
                return JSONResponse(status_code=400, content={"detail": "Missing path"})
            token = _issue_media_token(rel_path)
            # Log the PATH only - never the token (it is a credential).
            logger.info(f"Media token issued for {rel_path} (ttl={int(TTL_S)}s, max 3 uses)")
            return {"token": token, "expires_in": int(TTL_S)}
        except Exception as e:
            logger.error(f"Failed to issue media token: {e}")
            return JSONResponse(
                status_code=500,
                content={"detail": f"Failed to issue media token: {str(e)}"}
            )

    # ===== SETTINGS ENDPOINT =====
    @router.get("/api/settings")
    async def get_settings():
        """Return current client settings from settings.json.

        Returns the stored settings (server_address) plus the live-resolved address
        values so the UI always shows the active one. Used by the Settings modal.
        """
        try:
            from config import _load_settings, COOLEMS_CLIENT_SERVER_ADDRESS, _resolve_client_server_address, WEB_RELAY_UI_ENABLED, get_runtime_settings

            settings = _load_settings()

            # (2026-09-23) Connection Mode menu: always return the three fields so the UI can
            # render them even when never saved (defaults: direct / empty host / empty port).
            settings.setdefault("connection_mode", "direct")

            # (2026-09-23) Relay UI availability flag: when False the UI hides the whole
            # Connection Mode group; report direct so no relay state is displayed.
            settings["web_relay_ui_enabled"] = WEB_RELAY_UI_ENABLED
            if not WEB_RELAY_UI_ENABLED:
                settings["connection_mode"] = "direct"
            settings.setdefault("relay_host", "")
            settings.setdefault("relay_port", "")

            # (2026-10-02) Runtime tab: python_exec dialog behavior + auto-approve delay.
            # Read live from settings.json so the UI always shows the current values.
            runtime = get_runtime_settings()
            settings["python_exec_auto_approve"] = runtime["python_exec_auto_approve"]
            settings["python_exec_auto_approve_seconds"] = runtime["python_exec_auto_approve_seconds"]
            # (2026-10-03) Runtime tab: image generator tier selector.
            settings["image_gen_tier"] = runtime["image_gen_tier"]

            # Return both values:
            # 1. current_server_address - the value active at startup (module-level cached)
            # 2. resolved_server_address - freshly re-resolved NOW (reflects latest settings.json changes)
            settings["current_server_address"] = COOLEMS_CLIENT_SERVER_ADDRESS
            settings["resolved_server_address"] = _resolve_client_server_address()

            return settings
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}")
            return {"server_address": "", "current_server_address": ""}

    @router.post("/api/settings")
    async def save_settings(data: Dict):
        """Save client settings to settings.json.

        Supported fields:
          - connection_mode: "direct" (default) | "web_relay" — how the CLIENT talks to
            the SERVER. web_relay routes through the public web_server_relay (single-file PHP relay).
          - relay_host: str hostname/IP of the relay (required for web_relay mode)
          - relay_port: int 1-65535 (optional; WEB_RELAY_DEFAULT_PORT applies when empty)
          - server_address: str (IP:port format, e.g., "192.168.1.100:8080")
          - server_addresses: list[str] ordered failover addresses; index 0 is tried
            FIRST (put localhost there so local wins), the rest only when unreachable

        Runtime tab fields (2026-10-02):
          - python_exec_auto_approve: "manual" (default) | "auto" — python_exec dialog behavior
          - python_exec_auto_approve_seconds: int 1..3600 — auto-approve delay in seconds

        Runtime tab fields (2026-10-03):
          - image_gen_tier: "auto" (default) | "high" | "mid" | "low" — which model line
            generate_image() uses. 'auto' = classify by probed GPU VRAM; the others pin that
            tier (same semantics as SERVER config IMAGE_GEN_TIER_OVERRIDE, but per-machine).

        NOTE: Changes to server address(es) apply from the NEXT connection onward
        (the failover list is re-read on every connect); boot-time display values
        refresh on CLIENT restart.
        """
        try:
            from config import _load_settings, _save_settings, WEB_RELAY_UI_ENABLED

            # Load existing settings and merge with new values
            current = _load_settings()

            def _validate_addr(addr):
                """Return an error detail string for a bad address, or None when valid."""
                if ":" not in addr:
                    return "Server address must include port (e.g., 192.168.1.100:8080)"
                host_part = addr.rsplit(":", 1)[0]
                port_part = addr.rsplit(":", 1)[1]
                if not host_part:
                    return "Server address must have a non-empty host/IP (e.g., 192.168.1.100:8080)"
                if not port_part.isdigit():
                    return f"Port '{port_part}' is not a valid number. Must be between 1 and 65535."
                port_num = int(port_part)
                if not (1 <= port_num <= 65535):
                    return f"Port '{port_part}' is out of range. Must be between 1 and 65535."
                return None

            # (2026-09-23) Connection Mode menu fields.
            if "connection_mode" in data:
                mode = str(data["connection_mode"]).strip().lower()
                if mode not in ("direct", "web_relay"):
                    return JSONResponse(status_code=400, content={"detail": "connection_mode must be 'direct' or 'web_relay'"})
                if mode == "web_relay" and not WEB_RELAY_UI_ENABLED:
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "Internet Web Relay is disabled on this CLIENT (WEB_RELAY_UI_ENABLED=False in config/config.py)"}
                    )
                current["connection_mode"] = mode

            if "relay_host" in data:
                relay_host = str(data.get("relay_host") or "").strip()
                # basic hostname/IP sanity (no whitespace, no path)
                if relay_host and (any(c.isspace() for c in relay_host) or "/" in relay_host):
                    return JSONResponse(status_code=400, content={"detail": f"Invalid relay host {relay_host!r}"})
                current["relay_host"] = relay_host

            # (2026-10-02) Runtime tab fields: python_exec dialog behavior.
            #   python_exec_auto_approve        : "manual" (default, wait for the user) | "auto"
            #   python_exec_auto_approve_seconds: 1..3600 (dialog auto-runs after this many seconds)
            if "python_exec_auto_approve" in data:
                mode = str(data["python_exec_auto_approve"]).strip().lower()
                if mode not in ("manual", "auto"):
                    return JSONResponse(status_code=400, content={"detail": "python_exec_auto_approve must be 'manual' or 'auto'"})
                current["python_exec_auto_approve"] = mode

            if "python_exec_auto_approve_seconds" in data:
                raw_sec = str(data.get("python_exec_auto_approve_seconds") or "").strip()
                try:
                    sec = int(raw_sec)
                except (TypeError, ValueError):
                    return JSONResponse(status_code=400, content={"detail": "python_exec_auto_approve_seconds must be a whole number of seconds"})
                if not (1 <= sec <= 3600):
                    return JSONResponse(status_code=400, content={"detail": "python_exec_auto_approve_seconds must be between 1 and 3600"})
                current["python_exec_auto_approve_seconds"] = sec

            # (2026-10-03) Runtime tab field: image generator tier. generate_image reads this
            # LIVE from settings.json on every run, so the pick applies to the very next
            # generation - no restart needed.
            if "image_gen_tier" in data:
                tier = str(data["image_gen_tier"]).strip().lower()
                if tier not in ("auto", "high", "mid", "low"):
                    return JSONResponse(status_code=400, content={"detail": "image_gen_tier must be one of 'auto', 'high', 'mid', 'low'"})
                current["image_gen_tier"] = tier

            if "relay_port" in data:
                raw_port = str(data.get("relay_port") or "").strip()
                if raw_port == "":
                    current["relay_port"] = ""  # empty -> default port at connect time
                elif not raw_port.isdigit() or not (1 <= int(raw_port) <= 65535):
                    return JSONResponse(status_code=400, content={"detail": f"Relay port {raw_port!r} must be a number between 1 and 65535"})
                else:
                    current["relay_port"] = int(raw_port)

            # web_relay mode requires a relay host - fail fast with an actionable message.
            if current.get("connection_mode") == "web_relay" and not str(current.get("relay_host") or "").strip():
                return JSONResponse(status_code=400, content={"detail": "Internet Web Relay mode needs a relay host (IP or domain)"})

            if "server_address" in data:
                addr = str(data["server_address"]).strip()
                # Validate format - should be IP:port or hostname:port
                if addr:
                    err = _validate_addr(addr)
                    if err:
                        return JSONResponse(status_code=400, content={"detail": err})
                current["server_address"] = addr

            if "server_addresses" in data:
                # Ordered failover list - index 0 is tried FIRST (local first), the rest
                # only when the previous address cannot be reached. Empty list clears it.
                raw_list = data.get("server_addresses")
                items = raw_list if isinstance(raw_list, list) else [raw_list]
                cleaned = []
                for item in items:
                    addr = str(item).strip()
                    if not addr:
                        continue
                    err = _validate_addr(addr)
                    if err:
                        return JSONResponse(status_code=400, content={"detail": f"{addr}: {err}"})
                    if addr not in cleaned:
                        cleaned.append(addr)
                current["server_addresses"] = cleaned

            _save_settings(current)

            logger.info(f"Settings saved: {current}")

            return {"status": "ok", "message": "Settings saved successfully.", **current}
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            return JSONResponse(
                status_code=500,
                content={"detail": f"Failed to save settings: {str(e)}"}
            )

    # ===== SEARCH ENGINES ENDPOINTS =====
    # <CLIENT>/config/search_engines.json is the single source of truth for web search.
    # The Settings UI renders/edits it through these two endpoints; the web_search tool
    # reads the same file live on every search, so saves apply immediately (no restart).

    @router.get("/api/search-engines")
    async def get_search_engines():
        """Return the full engine list from config/search_engines.json.

        On a missing/corrupt file load_search_engines() returns [] and logs an error -
        the UI then shows that state instead of inventing engines (no fallback lists).
        """
        try:
            from config import load_search_engines

            return {"engines": load_search_engines()}
        except Exception as e:
            logger.error(f"Failed to read search engines: {e}")
            return JSONResponse(status_code=500, content={"detail": f"Failed to read search engines: {str(e)}"})

    @router.post("/api/search-engines")
    async def save_search_engines_endpoint(data: Dict):
        """Replace the engine list in config/search_engines.json.

        Expects {"engines": [ ... ]}. Every entry is validated (id format, name,
        enabled bool, priority int >= 1, settings object); on any error the file is
        left untouched and a 400 with the reason is returned.
        """
        try:
            from config import load_search_engines, save_search_engines

            engines = data.get("engines")
            if not isinstance(engines, list):
                return JSONResponse(status_code=400, content={"detail": "Body must be {'engines': [...]}"})

            try:
                save_search_engines(engines)
            except ValueError as ve:
                return JSONResponse(status_code=400, content={"detail": str(ve)})

            logger.info(f"Search engines saved: {len(load_search_engines())} engine(s)")
            return {"status": "ok", "message": "Search engines saved successfully.", "engines": load_search_engines()}
        except Exception as e:
            logger.error(f"Failed to save search engines: {e}")
            return JSONResponse(status_code=500, content={"detail": f"Failed to save search engines: {str(e)}"})

    @router.get("/api/models")
    async def get_models():
        """Return available models.

        Each model dict has:
          - name: the model filename
          - size: file size in bytes
          - on_disk: True
          - loaded: True if currently loaded in server

        ROLE-BASED FILTERING IS DONE ON THE SERVER: the provider's WS
        connection authenticates with the user's key, and the SERVER only
        returns models allowed for that role. No local filtering here.

        During a model switch the SERVER is down/reloading - an empty list then
        is EXPECTED, so it is logged as INFO (one clean line) instead of an ERROR.
        """
        try:
            # (2026-08-20) persistent control channel (async); role filtering still done on SERVER
            models = await provider.get_models_list_async()  # Already filtered by role on SERVER
        except Exception as e:
            logger.error(f"[MODELS] Failed to get models from provider ({type(provider).__name__}): {e}", exc_info=True)
            models = []

        if is_model_switching():
            # Expected state while the SERVER reloads - not an error. One clean INFO line, no traceback.
            logger.info(f"[MODELS] Model switch in progress - model list unavailable until the SERVER finishes reloading")

        return models

    @router.post("/api/llama/load-model")
    async def load_model(data: Dict):
        """Switch the currently loaded model in llama.cpp.

        Stops the current server and restarts it with the new model.
        Runs in background thread to avoid blocking the HTTP response.

        Expects: {"model": "ModelName.gguf"}
        Returns: {"status": "ok"|"error", "message": "..."}
        """
        import os as _os
        from app.websocket.connections import broadcast_model_switch_status

        model_filename = data.get("model", "").strip()
        if not model_filename:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Model filename is required"}
            )

        logger.info(f"[MODEL SWITCH] ===== POST RECEIVED: switching to {model_filename} =====")
        logger.info(f"[MODEL SWITCH] Provider type: {type(provider).__name__}")

        # --- GUARD 1: Block if a switch task is already running ---
        _model_switch_tasks[:] = [t for t in _model_switch_tasks if not t.done()]
        if _model_switch_tasks:
            logger.warning(f"[MODEL SWITCH] Blocked - {len(_model_switch_tasks)} switch task(s) still running")
            return JSONResponse(
                status_code=409,
                content={"status": "error", "message": "Model switch already in progress. Please wait."}
            )

        # --- GUARD 2: Skip if the requested model is already loaded ---
        current_model = None
        try:
            current_model = await provider.get_current_model_async()  # (2026-08-20) control channel
            logger.info(f"[MODEL SWITCH] Currently loaded model: {current_model}")
        except Exception as e:
            logger.warning(f"[MODEL SWITCH] Could not get current model: {e}")

        if current_model:
            basename_req = _os.path.basename(model_filename).lower()
            basename_cur = _os.path.basename(current_model).lower()
            # Compare with .gguf stripped for flexibility
            req_stripped = basename_req[:-5] if basename_req.endswith('.gguf') else basename_req
            cur_stripped = basename_cur[:-5] if basename_cur.endswith('.gguf') else basename_cur

            # FIX: Use exact match only. Substring matching was incorrectly blocking
            # switches between different quantizations of the same model
            # (e.g. Q6_K -> Q3_K_M both report as "Qwen3.6-27B-MTP" from the API).
            if req_stripped == cur_stripped:
                logger.info(f"[MODEL SWITCH] Model {model_filename} already loaded - skipping switch")
                return {
                    "status": "ok",
                    "message": f"Model '{model_filename}' is already loaded.",
                    "model": model_filename
                }

        # SET FLAG IMMEDIATELY — before any delay or background task creation.
        # This ensures periodic health checks suppress errors from the very start.
        _set_model_switching(True)

        # Broadcast "started" status to all connected UI clients (on main event loop ✓)
        await broadcast_model_switch_status("started", f"Switching model...", model_filename)

        # (2026-09 threadless refactor): _do_switch() is an ASYNC task on THIS loop -
        # there is no background thread anymore; it awaits provider.switch_model() directly.
        async def _do_switch():
            """Wait for the SERVER reload within the configured budget and report ONE clean result.

            No fallbacks: either the provider confirms the new model is loaded (success),
            or it reports why not (failure). Progress is logged every
            MODEL_SWITCH_PROGRESS_LOG_SEC while waiting.
            """
            from config import MODEL_SWITCH_MAX_WAIT_SEC, MODEL_SWITCH_PROGRESS_LOG_SEC
            start_time = asyncio.get_event_loop().time()

            try:
                logger.info(f"[MODEL SWITCH] Starting background switch to {model_filename} "
                            f"(will wait up to {MODEL_SWITCH_MAX_WAIT_SEC}s for the SERVER reload)")

                # Broadcast "reloading" status before doing the actual switch (on main event loop ✓)
                await broadcast_model_switch_status("reloading", f"Server reloading with {model_filename}", model_filename)

                # Progress broadcaster: runs on THIS (main) event loop as its own task and
                # sends UI updates + one clean progress log line while we await the switch below.
                progress_event = asyncio.Event()

                async def _progress_broadcaster():
                    next_log_at = MODEL_SWITCH_PROGRESS_LOG_SEC
                    while not progress_event.is_set():
                        await asyncio.sleep(1)
                        elapsed = int(asyncio.get_event_loop().time() - start_time)
                        if elapsed >= next_log_at:
                            logger.info(f"[MODEL SWITCH] Waiting for SERVER to reload {model_filename}... ({elapsed}s)")
                            try:
                                await broadcast_model_switch_status("waiting", f"Waiting... {elapsed}s", model_filename)
                            except Exception as bcast_err:
                                logger.debug(f"[MODEL SWITCH] Progress broadcast failed (UI clients may be gone): {bcast_err}")
                            next_log_at += MODEL_SWITCH_PROGRESS_LOG_SEC

                progress_task = asyncio.create_task(_progress_broadcaster())

                try:
                    # (2026-09 threadless refactor): direct await - switch_model() is async now.
                    # The old run_in_executor(None, ...) wrapper pushed the call onto a default executor
                    # thread that then spawned ANOTHER event loop inside the provider (double-threading).
                    # Awaiting it here keeps everything on this one loop; the progress broadcaster below
                    # still runs concurrently with the switch.
                    success, message = await provider.switch_model(model_filename)
                finally:
                    # Signal broadcaster to stop and let it finish cleanly
                    progress_event.set()
                    try:
                        await asyncio.wait_for(progress_task, timeout=3)
                    except asyncio.TimeoutError:
                        pass

                elapsed = int(asyncio.get_event_loop().time() - start_time)

                if success:
                    # Single authoritative success line (the provider already confirmed the model).
                    logger.info(f"[MODEL SWITCH] ===== SUCCESS: {model_filename} loaded in {elapsed}s =====")
                    await broadcast_model_switch_status("success", f"Model loaded ({elapsed}s)", model_filename)
                else:
                    # Provider returned a real failure reason (auth, file not found, SERVER timeout...).
                    logger.error(f"[MODEL SWITCH] FAILED after {elapsed}s: {message}")
                    await broadcast_model_switch_status("failed", message, model_filename)

            except Exception as e:
                elapsed = int(asyncio.get_event_loop().time() - start_time)
                # Unexpected error inside the switch task itself (not a SERVER-side failure).
                logger.error(f"[MODEL SWITCH] Unexpected exception after {elapsed}s: {type(e).__name__}: {e}", exc_info=True)
                await broadcast_model_switch_status("failed", f"{type(e).__name__}: {e}", model_filename)
            finally:
                try:
                    _model_switch_tasks.remove(task)
                except ValueError:
                    logger.debug("Caught expected value/key error")
                # Reset flag so periodic checks resume normally
                _set_model_switching(False)
                logger.info(f"[MODEL SWITCH] Task cleanup complete. Active switch tasks: {len(_model_switch_tasks)}")

        task = asyncio.create_task(_do_switch())
        _model_switch_tasks.append(task)
        logger.info(f"[MODEL SWITCH] Task created (id={id(task)}). Active switch tasks: {len(_model_switch_tasks)}")

        return {
            "status": "ok",
            "message": f"Model switch to {model_filename} started. Server will restart automatically.",
            "model": model_filename
        }

    @router.get("/api/ollama/status")
    async def get_ollama_status(request: Request):
        """Backward-compatible endpoint. Works with any provider."""
        # (2026-08-20) persistent control channel instead of a fresh connect+auth per poll
        is_healthy, error_message, models = await provider.health_check_async(force=True)

        role = getattr(request.state, 'api_role', 'unknown')

        return {
            "status": "healthy" if is_healthy else "unhealthy",
            "is_running": is_healthy,
            "error": error_message if not is_healthy else None,
            "models": models,
            "default_model": model_name,
            "provider": provider.name,
            "provider_url": provider.api_url,
            "timeout": api_timeout,
            "authenticated_role": role,
        }

    @router.get("/api/ollama/check")
    async def ollama_quick_check():
        """Backward-compatible quick health check."""
        is_healthy, _, _ = await provider.health_check_async()  # (2026-08-20) control channel
        return {"healthy": is_healthy}

    @router.get("/api/status")
    async def get_status(request: Request):
        """Return system status: context window, model, provider, role."""
        role = getattr(request.state, 'api_role', 'unknown')

        return {
            "context_window_tokens": _cfg_live.CONTEXT_WINDOW_TOKENS,  # live (2026-08-20)
            "model": model_name,
            "provider": provider.name,
            "authenticated_role": role,
        }

    @router.get("/api/agent/dna")
    async def get_agent_dna():
        """Get agent current DNA summary."""
        return {
            "dna_content": agent.get_identity(),
            "name": agent.get_name(),
            "personality": agent.get_personality(),
            "goals": agent.get_goals(),
        }

    @router.post("/api/agent/learn")
    async def agent_learn(data: Dict):
        """Teach the agent something new."""
        fact = data.get("fact", "").strip()
        if not fact:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Fact is required"}
            )
        agent.learn(fact)
        return {"status": "ok", "message": f"Learned: {fact}"}

    @router.post("/api/stop/{conv_id}")
    async def stop_generation(conv_id: str):
        """Stop the current generation for a conversation.

        STALE-TASK FIX (2026-09-10, "No active generation found" bug): this endpoint is
        now BUS-AWARE. The red stop button in the UI calls it whenever the chat shows as
        working - and that display state comes from the SAME ChatBus channel status
        (/api/conversations/{id}/status + chat_status frames). So when the button is red,
        a live channel with a running task ALWAYS exists here; we route through bus.stop()
        which sets the channel's stop event (cooperative abort at every loop boundary) and
        also CANCELS the background task (hard backstop for tool-execution windows that do
        not poll the event). The old code read only the legacy stop_events dict, whose
        entry is removed as soon as a turn ends - so after a crash/reopen it 404'd with
        "No active generation found" while the UI still showed the chat as working.

        Fallback (bus missing or no channel): the legacy stop_events dict path stays for
        compatibility, and if even that is empty we return an honest "nothing to stop"
        success - the caller's contract is that clicking Stop ALWAYS clears the red state,
        never errors on a ghost session.
        """
        from app.chat_bus import get_chat_bus

        bus = get_chat_bus()
        if bus is not None:
            stopped = await bus.stop(conv_id)
            if stopped:
                logger.info(f"Stop requested for conversation {conv_id} (via ChatBus)")
                return {"status": "ok", "message": "Generation stopped"}

        # Fallback path: legacy stop_events dict (bus unavailable or channel already gone).
        entry = stop_events.get(conv_id)
        if entry is not None:
            event = entry["event"] if isinstance(entry, dict) else entry
            try:
                event.set()
            except AttributeError as e:
                logger.error(f"Stop failed for conversation {conv_id}: bad stop_events entry type {type(entry).__name__}")
                return JSONResponse(
                    status_code=500,
                    content={"status": "error", "message": f"Internal error setting stop event: {e}"}
                )
            logger.info(f"Stop requested for conversation {conv_id} (via legacy stop_events)")
            return {"status": "ok", "message": "Generation stopped"}

        # No live session at all (ghost state after a crash/reopen, or the turn already
        # finished). Report success: there is nothing to abort and the UI must clear.
        logger.info(f"Stop requested for conversation {conv_id}: no active generation - nothing to stop")
        return {"status": "ok", "message": "No active generation - already stopped"}

    # ===== PYTHON_EXEC USER APPROVAL ENDPOINT (2026-09-24) ======================
    # The chat UI's approval card posts the user's decision here. It resolves the
    # pending asyncio.Future owned by this chat's ExecApprovalState (see
    # app/chat_bus/exec_approval.py). Same uvicorn event loop as the generation
    # task, so a plain set_result is safe - no cross-thread plumbing.

    @router.post("/api/exec-approval/{conv_id}/{request_id}")
    async def exec_approval_decision(conv_id: str, request_id: str, data: Dict):
        """Resolve a pending python_exec approval for one chat.

        Body: {"decision": "allow" | "deny" | "run_all"}
          - allow   : run this ONE piece of code
          - deny    : do NOT run it (the AI is told the user declined)
          - run_all : "run until task done" - every python_exec of THIS agentic
                      run executes without asking; resets on the next loop run

        409 when the request id is unknown or already answered (e.g. the user was
        too slow and it timed out, or the turn was stopped in between).
        """
        from app.chat_bus import get_chat_bus

        decision = str((data or {}).get("decision", "")).strip().lower()
        if decision not in ("allow", "deny", "run_all"):
            return JSONResponse(
                status_code=400,
                content={"status": "error",
                         "message": "decision must be one of: allow, deny, run_all"}
            )

        bus = get_chat_bus()
        channel = bus.channels.get(conv_id) if bus is not None else None
        state = getattr(channel, "exec_approval", None) if channel is not None else None
        if state is None or not hasattr(state, "resolve"):
            logger.info(f"[EXEC-APPROVAL] no live approval state for conv={conv_id} - nothing to resolve")
            return JSONResponse(
                status_code=409,
                content={"status": "error",
                         "message": "No pending python_exec approval for this workspace."}
            )

        if not state.resolve(request_id, decision):
            return JSONResponse(
                status_code=409,
                content={"status": "error",
                         "message": "That approval was already answered or expired."}
            )

        logger.info(f"[EXEC-APPROVAL] conv={conv_id} request={request_id} -> {decision}")
        return {"status": "ok", "decision": decision}

    @router.get("/api/my-permissions")
    async def get_my_permissions(request: Request):
        """Get current user's role.

        NOTE: The CLIENT does not resolve permissions locally — the SERVER
        enforces model/tool restrictions and pre-filters all lists it sends.
        This endpoint reports the authenticated role for display only.
        """
        role = getattr(request.state, 'api_role', 'unknown')

        return {
            "role": role,
            "allowed_tools": None,   # Enforced on SERVER (tools_response is role-filtered)
            "allowed_models": None,  # Enforced on SERVER (model lists are pre-filtered)
            "has_all_tools": True,   # Client sees no local restriction; server enforces real limits
            "has_all_models": True,
        }

    return router
