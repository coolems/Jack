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


        """Get all model folders to scan: default + allowed_models_folders from profiles.json.


    


        Returns ALL unique folders discovered across every role's allowed_models_folders lists.


        Role-based filtering is handled by the agent router (agent.py), not here.


        This function only scans disk for available models.


        """


        root_dir = _resolve_server_root()


        default_dir = os.path.join(root_dir, "llama_server", "models")


    


        # Always include the default directory first (highest priority)


        folders = [default_dir] if os.path.isdir(default_dir) else []


        seen = {os.path.abspath(default_dir)} if os.path.isdir(default_dir) else set()


    


        # Load profiles.json - check BOTH config/ and root locations


        profiles_path = None


        for candidate in [


            os.path.join(root_dir, "config", "profiles.json"),


            os.path.join(root_dir, "profiles.json"),


        ]:


            if os.path.exists(candidate):


                profiles_path = candidate


                break


    


        if not profiles_path:


            logger.debug("No profiles.json found for model folder discovery")


            return folders


    


        try:


            with open(profiles_path, 'r') as f:


                profiles_data = json.load(f)


    


            # Collect ALL unique folders from every role's allowed_models_folders.


            # Both list and null are handled - null means "all folders" for that role,


            # but we still need to scan all folders on disk regardless of role.


            # Role-based filtering happens in the agent router (agent.py get_models endpoint).


            for role_name, profile in profiles_data.items():


                allowed_folders = profile.get("allowed_models_folders", [])


    


                if isinstance(allowed_folders, list):


                    # Add each folder from the role's list


                    for folder in allowed_folders:


                        abs_folder = os.path.abspath(folder)


                        if abs_folder not in seen and os.path.isdir(abs_folder):


                            folders.append(abs_folder)


                            seen.add(abs_folder)


                elif allowed_folders is None:


                    # null means this role has access to ALL folders.


                    # No additional folders to add - we already collected them from roles with lists.


                    pass


        except Exception as e:


            raise RuntimeError(
                f'[HEALTH] Failed to read profiles.json for model folder discovery at {profiles_path}: {e}\n'
                f'Check that the file exists, is valid JSON, and has correct permissions.'
            ) from e


    


        return folders


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


        """Scan ALL model directories (default + profiles.json allowed folders) for .gguf files,
        excluding mmproj/projector files. Deduplicates by filename (first occurrence wins).

        Each entry carries a "folder" field: the basename of the folder it was found in.
        The FOLDER is the model identity used by auth_gateway exact-match authorization
        (2026-08-23): one folder = one model, even when the folder holds multiple files."""


        all_folders = _get_all_model_folders()





        seen_names = set()


        models = []





        for folder in all_folders:


            if not os.path.isdir(folder):


                continue





            gguf_files = glob.glob(os.path.join(folder, "*.gguf"))





            for fpath in sorted(gguf_files):


                fname = os.path.basename(fpath)





                # Skip mmproj/projector files


                if _is_mmproj_file(fname):


                    logger.debug(f"Skipping mmproj file: {fname}")


                    continue





                # Deduplicate by filename (first folder wins - default has priority)


                if fname in seen_names:


                    logger.debug(f"Skipping duplicate model: {fname} (already found)")


                    continue





                seen_names.add(fname)
                fsize = os.path.getsize(fpath)
                models.append({
                    "name": fname,
                    # Folder identity for auth exact-match (folder basename).
                    # One folder == one model: mmproj/tokenizer/multi-part files in the
                    # same folder all belong to this single model.
                    "folder": os.path.basename(os.path.abspath(folder)),
                    "size": fsize,
                    "on_disk": True,
                })





        logger.info(f"Found {len(models)} .gguf model(s) on disk across {len(all_folders)} folder(s)")


        return models