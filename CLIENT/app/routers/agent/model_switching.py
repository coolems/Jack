"""Model endpoints: /api/models, POST /api/llama/load-model (routing, not a reload),
provider health/status, the agent DNA summary and the role display route.

Nodes keep the model they were started with: selecting a model asks the SERVER whether a
node serves it. Every chat request independently routes using its own model key.
"""

from typing import Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import asyncio
import logging

# (2026-08-20 review #7): live reader instead of an import-time capture -- the SERVER
# rewrites CONTEXT_WINDOW_TOKENS after every auth via apply_auth_info().
import client_config.config as _cfg_live
from app.providers.coolems_client.client_provider import (
    _begin_model_switch,
    _end_model_switch,
)

logger = logging.getLogger("COOLEMS")


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:
    """Attach the model/status/settings endpoints to *router*."""
    routing_checks: dict[str, asyncio.Task] = {}

    @router.get("/api/models")
    async def get_models():
        """Models available to this caller - filtered by the SERVER for this role.

        Each entry: name (filename), size, on_disk, loaded, and thinking - what this model can be
        told about reasoning, discovered from the serving node's chat template (ThinkingPolicy):
        supported, can_disable, can_display, levels (none/low/medium/high), model_values,
        model_default, preserve, budget, enforced, source, hint. supported: false means no
        thinking keys are sent to that model and the UI chip reads "Think: n/a" with hint as the
        reason (ui-controls.js updateThinkingUI).
        """
        try:
            # (2026-08-20) persistent control channel (async); role filtering still done on SERVER
            models = await provider.get_models_list_async()  # Already filtered by role on SERVER
        except Exception as e:
            logger.error(f"[MODELS] Failed to get models from provider ({type(provider).__name__}): {e}", exc_info=True)
            models = []

        return models

    @router.post("/api/llama/load-model")
    async def load_model(data: Dict):
        """Return the routing verdict for this exact model key, without reloading."""
        value = data.get("model")
        model = value.strip() if isinstance(value, str) else ""
        if not model:
            return JSONResponse(status_code=400, content={"detail": "Model name is required"})

        async def check():
            _begin_model_switch()
            try:
                return await provider.switch_model(model)
            except Exception:
                logger.exception("Routing check failed for %s", model)
                return False, "Model routing check failed"
            finally:
                _end_model_switch()
                routing_checks.pop(model, None)

        # No await before registration: simultaneous requests for the same key share
        # a verdict; requests for different keys never wait on one another here.
        task = routing_checks.get(model)
        if task is None:
            task = routing_checks[model] = asyncio.create_task(check())
        success, message = await asyncio.shield(task)
        return {"status": "ok" if success else "error", "message": message, "model": model}

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


    @router.get("/api/balance")
    async def get_balance(request: Request, model: str = ""):
        """(2026-11 balance chip) Provider balance for the model loaded in this workspace.

        Visibility is decided on the SERVER: the profile flag show_provider_balance
        arrives in auth_ok and is cached by apply_auth_info(); when it is off this
        endpoint answers enabled=false and the number is never revealed. balance is
        null for local models (no billable account).
        """
        from app.keys import get_show_provider_balance
        if not get_show_provider_balance():
            return {"enabled": False, "balance": None}
        try:
            result = await provider.get_balance_async(model)
        except Exception as e:
            logger.warning(f"[BALANCE] Balance lookup failed: {e}")
            result = None
        if result is None:
            return {"enabled": True, "balance": None}
        return result

    @router.get("/api/agent/dna")
    async def get_agent_dna():
        """Agent identity summary (SERVER-delivered DNA, read-only)."""
        return {
            "dna_content": agent.get_identity(),
            "name": agent.get_name(),
            "personality": agent.get_personality(),
            "goals": agent.get_goals(),
        }
