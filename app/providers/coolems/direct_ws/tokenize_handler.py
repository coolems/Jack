"""tokenize_request handler: exact token counts via the local LlamaProvider."""

import json

from .log import logger


async def _handle_tokenize_request(ws, msg, server_provider):
    """Handle tokenize_request from a Direct WS client.

    Protocol:
        CLIENT -> {"type": "tokenize_request", "content": "<text>", "req_id": ...}
        SERVER -> {"type": "tokenize_response", "req_id": ..., "tokens": <int|null>}

    Counts tokens with the model's own tokenizer (llama.cpp /tokenize) so the client's
    budget math uses exact numbers instead of a chars/4 estimate. ``tokens`` is null when
    the tokenizer is unavailable (deepseek backend / llama down); the client falls back.
    """
    req_id = msg.get("req_id")
    content = msg.get("content", "") or ""

    tokens = None
    try:
        local = server_provider._get_local_provider()
        tok = getattr(local, "tokenize", None)
        if callable(tok):
            tokens = await tok(content)
    except Exception as e:
        logger.warning(f"[SERVER] tokenize_request failed: {e}")

    await ws.send(json.dumps({
        "type": "tokenize_response",
        "req_id": req_id,
        "tokens": tokens,
    }))
