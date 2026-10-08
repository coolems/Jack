"""Model endpoints: /api/models, POST /api/llama/load-model (the model switch with
GUARD 1 + GUARD 2 and the async _do_switch task), provider health/status endpoints,
agent DNA/learning and the role display endpoint.

MODEL SWITCH ERROR HANDLING (2026-08-20): while a switch is in progress,
'SERVER not answering' is an EXPECTED state - logged INFO/WARNING, never ERROR.
The background switch task waits for the SERVER reload within config.MODEL_SWITCH_*
budgets. No fallbacks: confirm the new model loaded or report one clear failure."""

from typing import Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import asyncio
import logging

# (2026-08-20 review #7): live reader instead of an import-time capture -- the SERVER
# rewrites CONTEXT_WINDOW_TOKENS after every auth via apply_auth_info().
import config.config as _cfg_live
from app.providers.coolems.client_provider import is_model_switching, _set_model_switching

logger = logging.getLogger("COOLEMS")

# Module-level list to keep references to background model switch tasks.
_model_switch_tasks: list = []


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:
    """Attach this endpoint group to *router* (verbatim move from the original flat file)."""
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
