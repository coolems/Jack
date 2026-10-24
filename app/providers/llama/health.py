"""


Health check and model listing for llama.cpp provider.


Uses shared sync HTTP session from http_client module instead of bare requests.get()
— enables TCP connection pooling across all health checks.


"""


import os


import glob


import json


import time


import asyncio


import logging


import requests


import httpx


from typing import List, Tuple, Dict





from config import HEALTH_CHECK_TIMEOUT





from .model_utils import _is_mmproj_file, normalize_model_name
from .path_utils import _resolve_server_root
from .http_client import get_async_client





logger = logging.getLogger("COOLEMS.Provider.Llama.Health")








    

# _resolve_server_root imported from .path_utils (single source of truth)

def _get_all_model_folders() -> list:
    """(2026-10 fleet) Local model scan dirs.

    Profiles no longer carry model folders (they authorize by filename), so the scan
    path lives in path_utils: llama_server/models (flat + one subfolder deep),
    config.LLAMA_MODEL_DIRS and the folder of the persisted .last_model_loaded.json.
    """
    from .path_utils import _get_model_scan_dirs
    return _get_model_scan_dirs()


class HealthChecker:


    """Caches health state and model list for llama.cpp."""





    def __init__(self, api_url: str, check_interval: float):


        self._api_url = api_url


        self._check_interval = check_interval


        self._is_healthy = True


        self._last_check = 0


        self._available_models: List[str] = []





    async def health_check(self, force: bool = False) -> Tuple[bool, str, List[str]]:
            """Check if llama.cpp is healthy. Returns (healthy, error_msg, models).

            (2026-09 threadless refactor): the old sync requests.Session version froze the event
            loop for up to HEALTH_CHECK_TIMEOUT per probe. It now uses the shared httpx.AsyncClient
            pool and awaits on the caller's loop."""
            current_time = time.time()
            if not force and self._is_healthy and (current_time - self._last_check) < self._check_interval:
                return True, "", self._available_models

            client = await get_async_client()
            try:
                resp = await client.get(f"{self._api_url}/health")
                if resp.status_code == 200:
                    try:
                        models_resp = await client.get(f"{self._api_url}/v1/models")
                        models_data = models_resp.json().get("data", [])
                        models = [m.get("id", "") for m in models_data]
                    except Exception:
                        models = []

                    self._is_healthy = True
                    self._last_check = current_time
                    self._available_models = models
                    logger.info(f"llama.cpp is healthy. Available models: {models}")
                    return True, "", models
                else:
                    error_msg = f"llama.cpp returned status code {resp.status_code}"
                    self._is_healthy = False
                    logger.warning(error_msg)
                    return False, error_msg, []
            except httpx.ConnectError:
                error_msg = "llama.cpp service is not running. Please start llama-server."
                self._is_healthy = False
                logger.error(error_msg)
                return False, error_msg, []
            except httpx.TimeoutException:
                error_msg = "llama.cpp service is not responding (timeout)."
                self._is_healthy = False
                logger.error(error_msg)
                return False, error_msg, []
            except Exception as e:
                error_msg = f"llama.cpp health check failed: {str(e)}"
                self._is_healthy = False
                logger.error(error_msg)
                return False, error_msg, []


    async def get_models_list(self) -> List[Dict]:
            """Fetch available models - returns ALL .gguf files from disk EXCEPT mmproj/OCR projector files.

            Each dict has:
              - name: the model filename (used as model id)
              - size: file size in bytes
              - on_disk: True (always, since we scan the disk)
              - loaded: True if this model is currently loaded in the server

            (2026-09 threadless refactor): the .gguf glob/scan is blocking filesystem work, so it
            runs off-loop via asyncio.to_thread; the /v1/models probe uses the shared httpx pool.
            """
            disk_models = await asyncio.to_thread(self._get_disk_models)

            # Also check what's currently loaded (async pooled client - no loop freeze)
            loaded_ids_normalized = set()
            try:
                client = await get_async_client()
                resp = await client.get(f"{self._api_url}/v1/models")
                models_data = resp.json().get("data", [])
                for m in models_data:
                    raw_id = m.get("id", "")
                    loaded_ids_normalized.add(normalize_model_name(raw_id))
                logger.info(f"[HEALTH] Loaded model IDs (normalized): {loaded_ids_normalized}")
            except Exception as e:
                logger.debug(f"[HEALTH] Could not fetch loaded models: {e}")

            for dm in disk_models:
                dm_norm = normalize_model_name(dm["name"])
                dm["loaded"] = dm_norm in loaded_ids_normalized

            return disk_models


    def _get_disk_models(self) -> List[Dict]:
        """Scan local model directories for .gguf files (flat + one subfolder deep).

        2026-10 fleet: profiles no longer carry model folders; path_utils owns the scan
        path. Entries are deduplicated by filename and mmproj/projector files are skipped.
        The "folder" field is kept for display and backward compatibility.
        """
        all_folders = _get_all_model_folders()
        seen_names = set()
        models = []
        for folder in all_folders:
            if not os.path.isdir(folder):
                continue
            for pattern in ("*.gguf", os.path.join("*", "*.gguf")):
                for fpath in sorted(glob.glob(os.path.join(folder, pattern))):
                    fname = os.path.basename(fpath)
                    if _is_mmproj_file(fname):
                        logger.debug("Skipping mmproj file: %s", fname)
                        continue
                    if fname in seen_names:
                        logger.debug("Skipping duplicate model: %s (already found)", fname)
                        continue
                    try:
                        fsize = os.path.getsize(fpath)
                    except OSError:
                        fsize = 0
                    seen_names.add(fname)
                    models.append({
                        "name": fname,
                        "folder": os.path.basename(os.path.abspath(folder)),
                        "size": fsize,
                        "on_disk": True,
                    })
        logger.info("Found %d .gguf model(s) on disk across %d folder(s)",
                    len(models), len(all_folders))
        return models
