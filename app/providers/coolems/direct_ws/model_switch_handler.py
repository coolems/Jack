"""Model switch request handler for direct WS clients.

Moved verbatim from ws_client_handler.py on 2026-09-07 (lines 557-602).
Permission check uses the shared exact-match resolver -- no substring auth.
"""

import json

from app.auth_gateway.model_resolver import is_model_allowed
from .log import logger

async def _handle_model_switch_direct(ws, server_provider, user_info, model_filename, req_id=None):
    """Handle model_switch message from direct WS client.

    req_id: echoed back in the response for the CLIENT persistent control channel (2026-08-20).
    """
    # Permission check: verify user is allowed to switch to this model.
    # (2026-08-23 security fix + dedupe): EXACT folder-name matching via the shared
    # auth_gateway resolver - no normalization, no bidirectional substring match.
    if user_info:
        local_check = server_provider._get_local_provider()
        all_models = await local_check.get_models_list()  # List of dicts already
        known_folders = {
            m["name"].lower(): m["folder"]
            for m in all_models
            if isinstance(m, dict) and m.get("name") and m.get("folder")
        }
        model_permitted = is_model_allowed(user_info, model_filename, known_folders)
        if not model_permitted:
            logger.warning("[SERVER] Model switch denied - no permission")
            await ws.send(json.dumps({
                "type": "model_switch_response",
                "req_id": req_id,
                "success": False,
                "message": f"Model '{model_filename}' is not available for your role."
            }))
            return

    local = server_provider._get_local_provider()
    try:
        # (2026-09 threadless refactor): direct await - switch_model() is async now and the
        # whole reload wait loop runs on this event loop (no executor thread, no nested loop).
        success, message = await local.switch_model(model_filename)
        await ws.send(json.dumps({
            "type": "model_switch_response",
            "req_id": req_id,
            "success": success,
            "message": message
        }))
    except Exception as e:
        logger.error(f"[SERVER] Model switch error: {e}", exc_info=True)
        await ws.send(json.dumps({
            "type": "model_switch_response",
            "req_id": req_id,
            "success": False,
            "message": str(e)
        }))
