"""ocr_request handler: SERVER-side OCR via the local LlamaProvider.

Moved verbatim from ws_client_handler.py on 2026-09-07 (lines 774-825).
The model-specific exact prompt format lives in app/providers/llama/ocr.py and
must stay exactly "Text Recognition:" -- the CLIENT never touches the model.
Only mechanical change: the llama import gained one dot of relative depth.
"""

import json
import os

from .log import logger

async def _handle_ocr_request(ws, msg, server_provider):
    """Handle ocr_request from a Direct WS client.

    Protocol:
        CLIENT -> {"type": "ocr_request", "image_base64": <b64>, "ocr_models": [names]}
        SERVER -> {"type": "ocr_response", "req_id": ..., "success": bool, "text": str}

    Runs on the SERVER where the local LlamaProvider can POST to llama-server with the
    model-specific exact call format (see app/providers/llama/ocr.py - the prompt must
    stay exactly "Text Recognition:"). The CLIENT never touches the model directly.
    """
    req_id = msg.get("req_id")
    image_base64 = msg.get("image_base64", "")
    ocr_models = list(msg.get("ocr_models") or [])

    # (2026-08-26) Tolerate clients that send no/unknown model names: resolve the
    # OCR model basenames from profiles.json at request time. The name only matters
    # for logging + error hints — llama-server serves whatever is loaded, and the
    # dedicated OCR fallback (ocr.py) picks the right instance itself.
    if not ocr_models:
        try:
            from ...llama.ocr_server_manager import resolve_ocr_candidates
            ocr_models = [os.path.basename(mp) for mp, _mm in resolve_ocr_candidates()]
            logger.info(f"[SERVER] OCR models resolved from profiles.json: {ocr_models}")
        except Exception as e:
            logger.warning(f"[SERVER] Could not resolve OCR models from profiles: {e}")

    if not image_base64 or not ocr_models:
        await ws.send(json.dumps({
            "type": "ocr_response",
            "req_id": req_id,
            "success": False,
            "text": "ERROR: OCR request missing 'image_base64' (or no OCR model resolvable from profiles.json)"
        }))
        return

    try:
        local = server_provider._get_local_provider()
        text = await local.ocr_transcribe(image_base64=image_base64, ocr_models=ocr_models)
        success = bool(text) and not str(text).startswith("ERROR")
        logger.info(f"[SERVER->CLIENT] OCR response: success={success}, {len(str(text))} chars (req_id={req_id})")
    except Exception as e:
        text = f"ERROR: SERVER-side OCR failed: {e}"
        success = False
        logger.error(f"[SERVER] OCR request error: {e}", exc_info=True)

    await ws.send(json.dumps({
        "type": "ocr_response",
        "req_id": req_id,
        "success": success,
        "text": text
    }))
