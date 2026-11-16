"""Short-lived single-use capability tokens for sub-resource URLs"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict

# Hard expiry for a minted token (seconds). Long enough for an <img> load + the
# user's click-to-open, short enough that a leaked URL is dead almost immediately.
TTL_S = 60.0

# Uses per token: covers the initial <img>/<iframe> load, one browser retry, and the
# "click to open full size" second hit on the same rendered element.
MAX_USES = 3

# Memory cap (tokens are tiny; 512 * ~60 bytes is negligible). Oldest entries evict.
MAX_TOKENS = 512

# The ONLY endpoints that accept ?t= tokens - both are read-only file surfaces.
MEDIA_TOKEN_PATHS = frozenset({"/api/download", "/api/open"})

# token -> [expiry_monotonic, uses_left, bound_path]
_tokens: "OrderedDict[str, list]" = OrderedDict()


def _purge_expired(now: float) -> None:
    """Drop expired/exhausted entries from the FRONT (oldest first)."""
    while _tokens:
        tok, entry = next(iter(_tokens.items()))
        if entry[0] > now and entry[1] > 0:
            break
        _tokens.popitem(last=False)


def issue_token(path: str) -> str:
    """Mint a token bound to *path*.

    The caller MUST already have passed header auth (the issuing endpoint sits behind
    APIMiddleware). Returns the opaque token string - log the PATH, never the token.
    """
    now = time.monotonic()
    _purge_expired(now)
    while len(_tokens) >= MAX_TOKENS:
        _tokens.popitem(last=False)
    tok = secrets.token_urlsafe(32)  # 256 bits of entropy, URL-safe alphabet
    _tokens[tok] = [now + TTL_S, MAX_USES, path]
    return tok


def consume_token(token: str, path: str) -> bool:
    """Single-consume check for *this exact* path.

    The token is removed from the store on EVERY attempt (valid or not): a failed
    probe does not leave it alive for another guess, and there is no way to test
    whether a leaked token still works without spending one of its uses. On success
    with remaining uses left, it is re-inserted with a decremented use count.
    """
    if not isinstance(token, str) or not token or len(token) > 128:
        return False
    entry = _tokens.pop(token, None)
    if entry is None:
        return False
    expiry, uses, bound_path = entry
    if time.monotonic() > expiry or uses <= 0 or bound_path != path:
        return False  # expired / exhausted / wrong file - token stays dead
    if uses - 1 > 0:
        _tokens[token] = [expiry, uses - 1, bound_path]
    return True


def clear() -> None:
    """Drop all tokens (tests only)."""
    _tokens.clear()
