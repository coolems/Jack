"""Model catalog — one list combining local disk models and fleet-served models.

2026-10 fleet: the brain may not hold every model on disk. Remote nodes run models the
brain has never downloaded (e.g. Gemma on a GPU box while the brain only has Qwen). The
CLIENT's model picker must still show them, so the catalog merges:

  * local .gguf files found by the llama provider (disk scan), and
  * every model any configured backend reports via /props.model_path.

Each entry is annotated with ``served_by`` (per-node slots/health), ``served`` (at least
one healthy node), ``online_slots`` and ``model_key``. Entries are deduplicated by the
canonical model key, preferring the disk entry (it carries size/folder).

The profile filter is applied LAST, so a user only sees the filenames their profile
allows. No folder logic here - authorization is by filename (see auth_gateway).
"""

import logging
from typing import List, Optional

from app.providers.llama.backend_registry import get_registry
from app.providers.llama.model_identity import model_key as model_key_of

logger = logging.getLogger("COOLEMS.Provider.ModelCatalog")


async def build_model_catalog(server_provider, user_info=None) -> List[dict]:
    """Return the model list for *user_info* (None = unfiltered, for internal checks)."""
    disk_models = []
    if server_provider is not None:
        try:
            local = server_provider._get_local_provider()
            disk_models = await local.get_models_list()
        except Exception as e:  # pragma: no cover - disk scan must never break auth/models
            logger.warning("[CATALOG] local model scan failed: %s", e)

    by_key: dict[str, dict] = {}
    for raw in disk_models:
        if not isinstance(raw, dict):
            continue
        key = model_key_of(raw.get("name", ""))
        if not key:
            continue
        entry = dict(raw)
        entry.setdefault("size", 0)
        entry["served_by"] = []
        entry["model_key"] = key
        by_key[key] = entry

    reg = get_registry()
    if reg is not None:
        for b in reg.backends.values():
            if not b.model_key:
                continue
            entry = by_key.get(b.model_key)
            if entry is None:
                entry = {
                    "name": b.model_label or b.model_key,
                    "size": 0,
                    "folder": None,
                    "on_disk": False,
                    "loaded": False,
                    "served_by": [],
                    "model_key": b.model_key,
                }
                by_key[b.model_key] = entry
            entry["served_by"].append({
                "name": b.instance.name,
                "host": b.instance.host,
                "port": b.instance.port,
                "slots": b.capacity,
                "healthy": bool(b.healthy),
                "busy": b.busy,
                "local": bool(b.local),
            })
            # "loaded" keeps its old meaning: loaded on the LOCAL node (the ⬤ marker).
            # Fleet availability is expressed by "served"/"online_slots" instead, so two
            # remote groups do not both pretend to be "the loaded model".
            if b.healthy and b.local:
                entry["loaded"] = True

    catalog = []
    for entry in by_key.values():
        served = entry.get("served_by") or []
        online_slots = sum(int(s.get("slots") or 0) for s in served if s.get("healthy"))
        entry["online_slots"] = online_slots
        entry["served"] = online_slots > 0
        entry.setdefault("loaded", False)
        catalog.append(entry)

    # Served models first, then alphabetical - stable picker order.
    catalog.sort(key=lambda e: (not e.get("served"), str(e.get("name", "")).lower()))

    if user_info is not None and server_provider is not None:
        try:
            catalog = server_provider._auth_gateway.get_allowed_models(user_info, catalog)
        except Exception as e:
            logger.error("[CATALOG] profile filter failed (%s) - returning no models", e)
            return []
    return catalog


def model_names(catalog: List[dict]) -> List[str]:
    """Plain filename list (used by auth_ok / logs)."""
    return [str(m.get("name", "")) for m in catalog if m.get("name")]


async def available_models_hint(server_provider, user_info=None) -> str:
    """User-filtered 'what can you use right now' hint for error frames.

    Never expose model names a profile is not allowed to see: the catalog is filtered
    before formatting. Server LOGS may still use registry.describe_models() (operator
    visibility), but client-facing messages use this.
    """
    catalog = await build_model_catalog(server_provider, user_info)
    served = [m for m in catalog if m.get("served")]
    if not served:
        return "no model is currently served"
    return ", ".join(
        f"{m.get('name')} ({m.get('online_slots', 0)} slot(s))" for m in served
    )
