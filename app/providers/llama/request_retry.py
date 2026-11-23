"""Retry an explicit request-JSON parse rejection before reading any SSE output."""

import json
import logging
import re
from contextlib import asynccontextmanager

logger = logging.getLogger("COOLEMS.Provider.Llama.Streaming")
_UNTERMINATED = re.compile(r"Unterminated string starting at: line \d+ column \d+ \(char \d+\)")


def _is_request_parse_rejection(response, body):
    if response.status_code != 400:
        return False
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return False
    return (isinstance(error, dict) and error.get("type") == "invalid_request_error"
            and isinstance(error.get("message"), str)
            and bool(_UNTERMINATED.fullmatch(error["message"])))


@asynccontextmanager
async def stream_with_request_retry(client, url, *, stop_event=None, conv_id=None, **kwargs):
    """Retry only the known pre-generation rejection, on the same backend and payload."""
    async with client.stream("POST", url, **kwargs) as response:
        if (response.status_code != 400 or (stop_event and stop_event.is_set())
                or not _is_request_parse_rejection(response, await response.aread())):
            yield response
            return
        if stop_event and stop_event.is_set():
            yield response
            return

    logger.warning("[REQUEST-JSON-RETRY] Backend rejected request JSON; resending once "
                   "(backend=%s, conv=%s)", url, conv_id)
    async with client.stream("POST", url, **kwargs) as response:
        if response.status_code == 200:
            logger.info("[REQUEST-JSON-RETRY] Backend accepted resend (conv=%s)", conv_id)
        else:
            logger.warning("[REQUEST-JSON-RETRY] Resend rejected (HTTP %s, conv=%s)",
                           response.status_code, conv_id)
        yield response
