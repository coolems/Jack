"""Profile Loader Module - Configuration file loading for auth gateway.

Loads .api_keys.json and profiles.json from the config directory.
Provides raw data that AuthGateway uses for authentication decisions.
"""

import json
import os
from typing import Dict, List, Any


def _resolve_config_dir() -> str:
    """Resolve SERVER config directory from this module's location.

    This file lives inside app/auth_gateway/ (a subpackage).
    Config is at project root /config/, so we go up TWO levels:
      app/auth_gateway/profile_loader.py  →  ../..  →  root/  →  config/
    """
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "config"
    )


def _seed_missing_config_file(config_dir: str, filename: str, example_name: str) -> None:
    """Create a missing config data file from its shipped example template.

    Never overwrites an existing file. Silently gives up when the example is
    absent or unreadable (the caller then raises its own actionable error).
    Kept local to this module on purpose - it must not create an import cycle
    with config/bootstrap.py which other server code imports at startup.
    """
    target = os.path.join(config_dir, filename)
    example_path = os.path.join(config_dir, example_name)
    try:
        if not os.path.exists(example_path):
            return
        with open(example_path, "r", encoding="utf-8") as f:
            raw = f.read()
        json.loads(raw)  # validate template before writing it anywhere
        with open(target, "w", encoding="utf-8") as f:
            f.write(raw)
    except (OSError, json.JSONDecodeError):
        pass


def load_api_keys_db(config_dir: str) -> List[Dict]:
    """Load .api_keys.json - returns list of user entries. Raises on failure."""
    path = os.path.join(config_dir, ".api_keys.json")
    if not os.path.exists(path):
        # Self-heal (2026-08-23): create from the example template so a fresh
        # checkout never crashes at startup. The seeded file contains only the
        # placeholder key - real keys must be added by the user before auth works.
        _seed_missing_config_file(config_dir, ".api_keys.json", ".api_keys.example.json")
        if not os.path.exists(path):
            raise FileNotFoundError(
                f'[AUTH] .api_keys.json not found at {path} and could not be created '
                f'from its example template. Check that config/ is writable.'
            )
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f'[AUTH] .api_keys.json contains invalid JSON at {path}: {e}. '
            f'Fix the file format before starting the server.'
        ) from e
    except OSError as e:
        raise RuntimeError(
            f'[AUTH] Failed to read .api_keys.json at {path}: {e}. '
            f'Check file permissions and disk availability.'
        ) from e

    if not isinstance(data, list):
        raise TypeError(
            f'[AUTH] .api_keys.json root must be a JSON array (list), got {type(data).__name__}. '
            f'Fix the file format before starting the server.'
        )
    return data


def load_blocked_libs_file(config_dir: str, filename: str) -> List[str]:
    """Load a python_exec blocked-libs set file (e.g. 'blocked_libs_set01.json').

    Returns the list of module names to block for import / from-import statements.
    An EMPTY list is meaningful: it means NO restrictions at all (admin clean set).

    FAIL-SAFE (never fail-open): missing file, unreadable file, invalid JSON or a
    wrong shape all return None -- callers then fall back to the built-in default
    blocklist instead of silently allowing everything.
    """
    if not filename:
        return None
    path = os.path.join(config_dir, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        # Caller logs; returning None triggers the built-in default blocklist.
        return None

    if isinstance(data, dict):
        modules = data.get("modules", [])
    elif isinstance(data, list):
        modules = data  # also accept a bare JSON array of module names
    else:
        return None

    if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
        return None
    return [m.strip() for m in modules if isinstance(m, str) and m.strip()]


def load_profiles_db(config_dir: str) -> Dict[str, Any]:
    """Load profiles.json - returns dict of role->config. Raises on failure."""
    path = os.path.join(config_dir, "profiles.json")
    if not os.path.exists(path):
        # Self-heal (2026-08-23): create from the example template so a fresh
        # checkout never crashes at startup. Seeded profiles use placeholder
        # model folders - edit them to point at your real models before use.
        _seed_missing_config_file(config_dir, "profiles.json", "profiles.example.json")
        if not os.path.exists(path):
            raise FileNotFoundError(
                f'[AUTH] profiles.json not found at {path} and could not be created '
                f'from its example template. Check that config/ is writable.'
            )
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f'[AUTH] profiles.json contains invalid JSON at {path}: {e}. '
            f'Fix the file format before starting the server.'
        ) from e
    except OSError as e:
        raise RuntimeError(
            f'[AUTH] Failed to read profiles.json at {path}: {e}. '
            f'Check file permissions and disk availability.'
        ) from e

    if not isinstance(data, dict):
        raise TypeError(
            f'[AUTH] profiles.json root must be a JSON object (dict), got {type(data).__name__}. '
            f'Fix the file format before starting the server.'
        )
    return data
