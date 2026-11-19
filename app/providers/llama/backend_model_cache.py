"""Last-known model cache for the LLM backend registry.

llama/strata backends are only discovered when the endpoint answers /props, and DeepSeek
endpoints only when GET /models answers. A DOWN endpoint therefore contributes nothing at
boot, so its models would vanish from the picker instead of showing up grayed/disabled.
This module persists the last models each backend served, so ``build_model_catalog()`` can
still list them as offline (``served=False``, ``online_slots=0``).

File: config/.backend_models_cache.json (gitignored, per-machine runtime state).

The cache is merged, not overwritten wholesale: a backend that answered this run replaces
its own cached entry (so a model genuinely removed from a healthy endpoint is pruned), while
a backend that did NOT answer keeps its previous entry (so a down endpoint's models survive
a restart). A missing/corrupt file never breaks boot - it just means no offline models.
"""

import json
import logging
import os
import tempfile

logger = logging.getLogger("COOLEMS.Provider.Llama.Cache")

# This module lives in <root>/app/providers/llama/ -> up 3 levels to root -> config/.
_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config"
)

CACHE_FILE_NAME = ".backend_models_cache.json"

_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _cache_path() -> str:
    """Absolute path of the last-known models cache in the server config folder."""
    return os.path.join(_CONFIG_DIR, CACHE_FILE_NAME)


def _identity(kind: str, host: str, port, base_url) -> str:
    """Stable cache key for one backend: deepseek by base_url, others by host:port."""
    if kind == "deepseek":
        return f"deepseek|{(base_url or '').rstrip('/').lower()}"
    return f"{kind}|{(host or '').strip().lower()}:{port}"


def _read() -> dict:
    """Read the cache file; {} when missing, unreadable or corrupt. Never raises."""
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(backends: dict) -> bool:
    """Atomically write the cache. Best-effort: a failure is logged, never raised."""
    payload = {"version": 1, "backends": backends}
    try:
        os.makedirs(os.path.dirname(_cache_path()), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=CACHE_FILE_NAME + ".", suffix=".tmp", dir=os.path.dirname(_cache_path())
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp_path, _cache_path())  # atomic on Windows + POSIX
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
    except Exception as e:
        logger.warning("[CACHE] Failed to write %s: %s", _cache_path(), e)
        return False
    return True


def remember(registry) -> bool:
    """Merge the registry's CURRENTLY-DISCOVERED models into the cache.

    Walks every DeepSeek endpoint (its /models list) and every llama/strata backend (its
    discovered model key). A backend that produced a discovery replaces its cached entry;
    one that did not keeps whatever was cached before. Returns True when the cache changed.
    """
    fresh: dict = {}

    for e in getattr(registry, "endpoints", {}).values():
        models = [
            {"model_key": m["key"], "label": m["id"], "aliases": []}
            for m in getattr(e, "endpoint_models", []) if m.get("key")
        ]
        if not models:
            continue
        fresh[_identity("deepseek", "", None, e.api_url)] = {
            "kind": "deepseek",
            "name": e.instance.name,
            "base_url": e.api_url,
            "models": models,
        }

    for b in getattr(registry, "backends", {}).values():
        # DeepSeek children are covered by their endpoint above; only top-level
        # llama/strata backends carry their own single model.
        if b.parent is not None or b.is_deepseek:
            continue
        if not b.model_key:
            continue
        fresh[_identity(b.kind, b.instance.host, b.instance.port, None)] = {
            "kind": b.kind,
            "name": b.instance.name,
            "host": b.instance.host,
            "port": b.instance.port,
            "models": [{
                "model_key": b.model_key,
                "label": b.model_label or b.model_key,
                "aliases": sorted(b.model_aliases),
            }],
        }

    cached = _read().get("backends", {})
    if not isinstance(cached, dict):
        cached = {}

    merged: dict = {}
    for identity, entry in cached.items():
        merged[identity] = fresh.get(identity, entry) if isinstance(entry, dict) else entry
    for identity, entry in fresh.items():
        merged.setdefault(identity, entry)

    if merged == cached:
        return False
    return _write(merged)


def remembered_models() -> list:
    """Catalog-shaped offline entries for every remembered model.

    Each entry is a down model: ``served_by`` carries exactly one unhealthy node, so the
    catalog's existing ``served=False``/``online_slots=0`` math grays it out. ``build_model_catalog``
    merges these only when no live backend already advertises the same model key.
    """
    out = []
    cached = _read().get("backends", {})
    if not isinstance(cached, dict):
        return out

    for entry in cached.values():
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind", "")
        host = str(entry.get("host") or "")
        port = entry.get("port")
        name = str(entry.get("name") or "")
        local = host in _LOOPBACK
        served_by = {
            "name": name or host or kind,
            "host": host,
            "port": port,
            "slots": 0,
            "healthy": False,
            "busy": 0,
            "local": local,
        }
        for m in entry.get("models", []):
            if not isinstance(m, dict):
                continue
            key = m.get("model_key")
            if not key:
                continue
            out.append({
                "name": m.get("label") or key,
                "size": 0,
                "folder": None,
                "on_disk": False,
                "loaded": False,
                "model_key": key,
                "aliases": sorted(m.get("aliases") or []),
                "served_by": [dict(served_by)],
                "offline": True,
            })
    return out
