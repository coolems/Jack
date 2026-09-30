"""Persistence for the last-loaded llama.cpp model — SOURCE OF TRUTH across restarts.

The SERVER config folder (config/) holds ``.last_model_loaded.json`` — a small JSON
file that records which model was most recently loaded into llama-server.

  * server.py writes it after EVERY verified launch / model switch.
  * On startup, start_server() reads it and loads THAT model first.
  * If the persisted model is not available in the active profile's allowed folders,
    start_server() falls back to the FIRST model found in those profile folders.

File shape (config/.last_model_loaded.json):
    {
      "model": "Qwen3.6-27B-MTP-Q6_K.gguf",                       # basename of loaded .gguf
      "path":  "<MODELS_ROOT>\\my-model-folder\\model.gguf"        # example: full path (per-machine, informational)
    }

The file is per-machine runtime state (gitignored). A missing/corrupt file never
breaks startup — callers simply fall back to their next resolution strategy.
"""

import json
import logging
import os
import tempfile
from typing import Optional, Tuple

logger = logging.getLogger("COOLEMS.Provider.Llama.Server")

# This module lives in <root>/app/providers/llama/ → up 3 levels to root → config/.
_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config"
)

LAST_MODEL_FILE_NAME = ".last_model_loaded.json"


def get_last_model_file() -> str:
    """Absolute path of the .last_model_loaded.json file in the server config folder."""
    return os.path.join(_CONFIG_DIR, LAST_MODEL_FILE_NAME)


def read_last_model() -> Tuple[Optional[str], Optional[str]]:
    """Read (model_basename, full_path) from .last_model_loaded.json.

    Returns (None, None) when the file is missing, unreadable or corrupt —
    callers then fall back to their other resolution strategies. Never raises.
    """
    path = get_last_model_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None

    if not isinstance(data, dict):
        logger.warning("[LAST-MODEL] %s has unexpected shape - ignoring it", path)
        return None, None

    model = data.get("model")
    full_path = data.get("path")
    if not isinstance(model, str) or not model.strip():
        return None, None
    if not isinstance(full_path, str):
        full_path = None
    return model.strip(), (full_path.strip() or None)


def write_last_model(model_basename: str, full_path: Optional[str] = None) -> bool:
    """Persist the last successfully loaded model. Atomic write (tmp file + replace).

    Never raises — a persistence failure must not break model loading; it is logged.
    Returns True on success.
    """
    path = get_last_model_file()
    payload = {
        "model": model_basename,
        "path": full_path or "",
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=LAST_MODEL_FILE_NAME + ".", suffix=".tmp", dir=os.path.dirname(path)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, path)  # atomic on Windows + POSIX
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
    except Exception as e:
        logger.error("[LAST-MODEL] Failed to write %s: %s", path, e)
        return False

    logger.info(
        "[LAST-MODEL] Persisted last loaded model: %s (%s)",
        model_basename, full_path or "path unknown",
    )
    return True


def get_loaded_model_name() -> str:
    """Return the persisted model basename, or "" when nothing is recorded yet."""
    name, _ = read_last_model()
    return name or ""
