"""Exact token counting for the agentic context budget.

The CLIENT has no local tokenizer, but the SERVER exposes the loaded model's real one
through ``provider.tokenize()`` (llama.cpp ``/tokenize`` over the WS relay). This module
turns that into per-message cached exact counts so the budget chain never has to guess
with the ``chars/4`` prose heuristic - which under-counts dense tool output (logs, code,
grep/diff results) by ~2x and is what let a single 477K-char ``read_file`` result pass the
trim/guardian gate and reach llama.cpp as a 321K-token prompt (HTTP 400).

Cache contract:
  - ``msg["_exact_tokens"]`` = exact count from the server tokenizer (authoritative).
  - ``utils.history_manager.estimate_message_tokens`` prefers it; ``chars/4`` is only the
    fallback for messages that are too small to matter or when the tokenizer is down.
  - ``invalidate_message_tokens`` drops BOTH caches on in-place content mutation.

Only messages at or above ``EXACT_COUNT_MIN_CHARS`` are exact-counted: a smaller message's
``chars/4`` absolute error is a few hundred tokens at most against a ~235K budget, while
every exact count costs a short-lived WebSocket round-trip.
"""

import json
import logging

from utils.history_manager import estimate_message_tokens, _sanitize_base64_in_string

logger = logging.getLogger("COOLEMS.TokenCounter")

# Messages shorter than this keep the chars/4 estimate. Below this size the worst-case
# dense-text error (len/4 tokens) is <= 500 tokens, which is noise against the history
# budget; skipping them keeps the initial-history tokenize pass to a handful of round-trips.
EXACT_COUNT_MIN_CHARS: int = 2000


def _message_text(msg: dict) -> str:
    """The text whose token count represents this message's prompt content cost.

    Content + tool_calls only. Role/chat-template framing and image tokenisation are NOT
    included: the server counts those as a near-constant overhead that the anchor
    (``react_loop.anchor_overhead``) already captures, so mixing them in here would
    double-count. ``image_url`` list parts are skipped for the same reason (the estimate
    counts them as a fixed per-image cost, not text).
    """
    parts = []
    content = msg.get("content")
    if isinstance(content, str):
        s = content
        if "base64" in s.lower() and len(s) > 10000:
            s = _sanitize_base64_in_string(s)
        parts.append(s)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
            else:
                parts.append(str(part))
    tcs = msg.get("tool_calls")
    if tcs:
        try:
            parts.append(json.dumps(tcs))
        except (TypeError, ValueError):
            parts.append(str(tcs))
    return "\n".join(p for p in parts if p)


def _approx_chars(msg: dict) -> int:
    """Cheap upper-bound size of a message's countable text (no full sanitize pass)."""
    n = 0
    content = msg.get("content")
    if isinstance(content, str):
        n += len(content)
    elif isinstance(content, list):
        for part in content:
            n += len(part.get("text", "")) if isinstance(part, dict) else len(str(part))
    tcs = msg.get("tool_calls")
    if tcs:
        n += len(tcs) if isinstance(tcs, str) else len(str(tcs))
    return n


def _needs_exact_count(msg: dict) -> bool:
    """True when a message is large enough for chars/4's ~2x error to matter."""
    if not isinstance(msg, dict):
        return False
    cached = msg.get("_exact_tokens")
    if isinstance(cached, int) and cached >= 0:
        return False
    return _approx_chars(msg) >= EXACT_COUNT_MIN_CHARS


async def _tokenize_or_none(provider, text: str):
    """Exact token count of *text* via the server, or None when it is unavailable.

    Never raises: a dead tokenizer must degrade to the chars/4 fallback, not break the
    budget chain. Returns None for a non-int/negative result (server sent something odd).
    """
    tok = getattr(provider, "tokenize", None) if provider is not None else None
    if not callable(tok):
        return None
    try:
        n = await tok(text)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[TOKEN-COUNTER] exact tokenize raised ({e}); chars/4 fallback")
        return None
    return n if isinstance(n, int) and n >= 0 else None


async def exact_message_tokens(provider, msg: dict):
    """Exact token count of one message (cached on ``msg["_exact_tokens"]``), or None.

    Returns None when the tokenizer is unavailable so the caller can short-circuit the
    rest of a pass instead of paying a per-message timeout. Does NOT write the fallback
    estimate into the cache - estimate_message_tokens already falls back to chars/4.
    """
    if isinstance(msg, dict):
        cached = msg.get("_exact_tokens")
        if isinstance(cached, int) and cached >= 0:
            return cached

    text = _message_text(msg) if isinstance(msg, dict) else str(msg)
    n = await _tokenize_or_none(provider, text)
    if n is None:
        return None

    if isinstance(msg, dict):
        try:
            msg["_exact_tokens"] = n
        except Exception:
            pass  # immutable dict - counting still works, just uncached
    return n


async def refresh_exact_token_cache(provider, messages) -> int:
    """Exact-count messages lacking ``_exact_tokens``; return the (now exact) total.

    Sequential with early-out: the first unavailable tokenizer aborts the pass so a down
    server cannot turn N messages into N x 15s timeouts. Already-counted messages are a
    dict read, so per-iteration cost is only the 1-3 messages appended since the last pass.
    """
    pending = [m for m in messages if _needs_exact_count(m)]
    for m in pending:
        if await exact_message_tokens(provider, m) is None:
            break

    # estimate_message_tokens now prefers _exact_tokens, so this sum is exact where set.
    return sum(estimate_message_tokens(m) for m in messages)
