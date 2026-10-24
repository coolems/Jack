"""Model ROUTING request handler for direct WS clients (2026-10 fleet).

There is NO model switching any more: every llama node is started with a fixed model and
keeps it. The CLIENT's "load model" action therefore becomes a routing check - it reports
whether the swarm currently serves the requested model, and where. The actual routing
happens per chat request in the server queue (per-model FIFO).

Permission check still uses the shared exact-filename resolver (no folders, no substrings).
"""

import json

from app.auth_gateway.model_resolver import is_model_allowed
from app.providers.llama.model_identity import model_key as model_key_of
from .log import logger


def describe_routing(reg, model_filename: str, available_hint: str = "") -> tuple[bool, str]:
    """Shared by the direct and relay handlers: (ok, message) for a model request.

    *available_hint* must be the CALLER-FILTERED model list (never leak models the
    profile is not allowed to see); when empty the registry-wide list is used.
    """
    if reg is None:
        return False, "LLM backend registry is not initialized yet - please retry."
    key = model_key_of(model_filename)
    nodes = [b for b in reg.backends.values() if b.model_key == key]
    healthy = [b for b in nodes if b.healthy]
    if healthy:
        where = ", ".join(f"{b.instance.name} ({b.capacity} slot(s))" for b in healthy)
        return True, (f"Model '{model_filename}' is served by {where}. Requests are routed "
                      f"automatically - no reload is needed.")
    if nodes:
        where = ", ".join(
            f"{b.instance.name} ({b.instance.host}:{b.instance.port}, "
            f"{'healthy' if b.healthy else 'offline'})"
            for b in nodes
        )
        return False, (f"Model '{model_filename}' is configured but no node is serving it "
                       f"right now: {where}. Fix or start a node with that model.")
    return False, (f"No llama node is serving '{model_filename}'. Available: "
                   f"{available_hint or reg.describe_models()}. Start a node with that model to use it.")


async def _handle_model_switch_direct(ws, server_provider, user_info, model_filename, req_id=None):
    """Handle a 'model_switch' frame as a ROUTING CHECK (never reloads anything)."""
    if user_info and not is_model_allowed(user_info, model_filename):
        logger.warning("[SERVER] Model request denied for role=%s: %s",
                       user_info.get("role"), model_filename)
        await ws.send(json.dumps({
            "type": "model_switch_response",
            "req_id": req_id,
            "success": False,
            "message": f"Model '{model_filename}' is not available for your role.",
        }))
        return

    from app.providers.llama.backend_registry import get_registry
    from ..model_catalog import available_models_hint
    hint = await available_models_hint(server_provider, user_info)
    success, message = describe_routing(get_registry(), model_filename, available_hint=hint)
    logger.info("[SERVER] Model routing check for '%s': %s", model_filename, message)
    await ws.send(json.dumps({
        "type": "model_switch_response",
        "req_id": req_id,
        "success": success,
        "message": message,
    }))
