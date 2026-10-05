"""Short-lived single-use capability tokens for sub-resource URLs (SECURITY fix 2026-10-05).

Background
----------
<img>/<iframe>/anchor tags cannot send custom headers, so the UI used to pass the
MASTER API key as ?api_key=... on /api/download and /api/open. Query strings are part
of the request line: they land in browser history, the Network tab, Referer headers
leaked to third-party origins, and any logging hop - a long-lived credential (file
R/W + code execution on this machine) was exposed far beyond TLS-protected headers.

This module replaces that with capability tokens (?t=<token>):
  * random 256-bit value from secrets.token_urlsafe(32), in-memory only, never logged;
  * bound to ONE exact file path - a token minted for "a.txt" cannot be replayed
    against "b.txt";
  * short-lived (TTL_S) and limited-use (MAX_USES covers <img> retry + click-to-open);
  * consumed on every ATTEMPT (valid or not), so an attacker probing a leaked token
    gets no "still valid?" oracle.

Issuance happens at POST /api/auth/media-token, which the APIMiddleware only reaches
AFTER header auth passed - minting a token grants nothing beyond what the caller
already proved. The middleware accepts ?t= ONLY on MEDIA_TOKEN_PATHS and early-returns
with sentinel state; it never calls note_presented_key() for tokens, so a leaked
token can never fill the outbound-provider runtime credential store.

Run standalone:  python -c "import sys; sys.path.insert(0, 'CLIENT'); import app.media_tokens"
"""

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
