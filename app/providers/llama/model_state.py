"""Authoritative in-memory state of which model each llama-server instance has loaded.

server.py knows the model file it launched after every verified start_server. This module
exposes that knowledge as a small thread-safe cache, so the streaming layer pays an HTTP
round-trip only when the cache is empty or older than LLAMA_MODEL_STATE_TTL_SEC:
  - mark_loaded(name[, key])    : called by server.py right after a verified launch
  - invalidate([key])           : called when a start fails (state unknown again)
  - get_cached_model(ttl[, key]): returns (raw_name, fresh_within_ttl?)

Keyed per instance: key = (host, port), so every entry in config/llama_servers.json has
its own state and a request for model X is matched against the node serving X.
"""

import logging
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger("COOLEMS.Provider.Llama.Server")


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def key_for_api_url(api_url: str) -> Optional[Tuple[str, int]]:
    """Derive the (host, port) model-state key from an api_url like 'http://127.0.0.1:5000'.

    Returns None when the URL cannot be parsed — callers then use the legacy
    unkeyed path (global entry), which is exactly today's behavior for odd inputs.
    """
    if not api_url:
        return None
    url = api_url.strip()
    # strip scheme
    if "://" in url:
        url = url.split("://", 1)[1]
    host_port = url.split("/", 1)[0]
    if ":" not in host_port:
        return None
    host, _, port_s = host_port.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        return None
    if not host or not (0 < port < 65536):
        return None
    return (host.lower(), port)


# ---------------------------------------------------------------------------
# State storage
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# keyed entries: (host, port) -> (raw_model_name, updated_at)
_states: dict[Tuple[str, int], Tuple[Optional[str], float]] = {}
# legacy/unkeyed entry — keeps old call sites (and tests) working verbatim.
_global: Tuple[Optional[str], float] = (None, 0.0)

# ---------------------------------------------------------------------------
# Public API — keyed
# ---------------------------------------------------------------------------

def mark_loaded(model_name: str, key: Optional[Tuple[str, int]] = None) -> None:
    """Record that a llama-server instance is running with `model_name`.

    Called from server.py after a successful, verified launch (we know the exact
    file we put on disk into the process - no HTTP needed to confirm it).

    *key* identifies the instance ((host, port)); omit it for the global entry.
    """
    global _global
    with _lock:
        if key is None:
            _global = (model_name or None, time.time())
        else:
            _states[key] = (model_name or None, time.time())


def invalidate(key: Optional[Tuple[str, int]] = None) -> None:
    """Forget what is loaded.

    *key* given  -> only that instance's entry is cleared (its reload failed mid-way).
    *key* omitted -> EVERYTHING is cleared (legacy semantics — state unknown again).
    """
    global _global
    with _lock:
        if key is None:
            _states.clear()
            _global = (None, 0.0)
            logger.debug("[MODEL-STATE] Cache invalidated (all instances)")
        else:
            _states.pop(key, None)
            logger.debug("[MODEL-STATE] Cache invalidated for %s:%d", key[0], key[1])


def get_cached_model(ttl_sec: int, key: Optional[Tuple[str, int]] = None) -> Tuple[Optional[str], bool]:
    """Return (raw_model_name, is_fresh).

    is_fresh=True means the entry was written within ttl_sec seconds and can be
    trusted without any HTTP verification. A stale/empty cache returns
    (name?, False); callers should refresh via /v1/models once before comparing.

    With *key*: that instance's entry; when no keyed entry exists yet it falls back
    to the global entry. Without *key*: the global entry directly.
    """
    with _lock:
        if key is not None and key in _states:
            raw, ts = _states[key]
        else:
            raw, ts = _global
        fresh = bool(raw) and (time.time() - ts) < ttl_sec
        return raw, fresh
