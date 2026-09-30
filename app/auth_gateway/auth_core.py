"""Auth Core Module - Main AuthGateway class for SERVER-side authentication.

Coordinates profile loading, model resolution, and permission filtering.
Provides TTL-based caching to avoid reloading JSON files on every auth call.
"""

import hmac
import logging
import os
import time
from typing import Any, Dict, List, Optional

from config import PROFILE_DEFAULT_RATE_LIMIT

logger = logging.getLogger("COOLEMS.AuthGateway")


def _keys_equal(stored_key, provided_key: str) -> bool:
    """Constant-time API key comparison (H2 fix).

    Plain `==` short-circuits on the first differing byte, leaking how much of a
    guessed prefix is correct via timing. hmac.compare_digest compares in constant
    time; both sides are utf-8 encoded so str/bytes input shapes behave identically
    and non-string stored values can never crash it.
    """
    if not isinstance(stored_key, (str, bytes)) or not isinstance(provided_key, str):
        return False
    a = stored_key.encode("utf-8") if isinstance(stored_key, str) else stored_key
    b = provided_key.encode("utf-8")
    return hmac.compare_digest(a, b)


class AuthGateway:
    """Smart gateway that enforces authentication + profile-based permissions.

    Usage in server_provider.py:
        gateway = AuthGateway()
        user_info = gateway.authenticate(api_key)  # Returns dict or None
        if not user_info:
            reject_connection()
        allowed_models = gateway.get_allowed_models(user_info, all_models_from_llm)
        allowed_tools = gateway.get_allowed_tools(user_info, requested_tools)
    """

    def __init__(self):
        self._config_dir = _resolve_config_dir()
        # TTL-based cache to avoid reloading JSON files on every auth call
        self._api_keys_cache_ttl = 60  # seconds
        self._last_reload_time: float = 0.0
        self._file_stats: Dict[str, tuple] = {}  # path -> (mtime, size)
        # Load once at init

        self._api_keys_db: List[Dict] = []
        self._profiles_db: Dict[str, Any] = {}

        self._reload_databases()

    def _get_file_stat(self, path: str) -> tuple:
        """Get (mtime, size) for a file, or (0, 0) if it doesn't exist."""
        try:
            stat = os.stat(path)
            return (stat.st_mtime, stat.st_size)
        except OSError:
            return (0, 0)

    def _should_reload(self) -> bool:
        """Check if databases should be reloaded based on TTL and file mtime."""
        now = time.time()
        # Always reload if TTL expired or first call
        if now - self._last_reload_time >= self._api_keys_cache_ttl:
            return True
        # Check if files were modified since last load
        for filename in [".api_keys.json", "profiles.json"]:
            path = os.path.join(self._config_dir, filename)
            current_stat = self._get_file_stat(path)
            cached_stat = self._file_stats.get(filename, (0, 0))
            if current_stat != cached_stat:
                return True
        return False

    def _reload_databases(self):
        """Reload both databases from disk with caching metadata update.

        Uses atomic assignment to prevent partial state corruption if one file fails.
        Preserves old cache data on failure so auth continues working until files are fixed.
        """
        try:
            # Load into local variables first - prevents partial state corruption
            new_api_keys = load_api_keys_db(self._config_dir)
            new_profiles = load_profiles_db(self._config_dir)

            # Only assign if BOTH loads succeeded (atomic update)
            self._api_keys_db = new_api_keys
            self._profiles_db = new_profiles

            # Update cache state after successful reload
            for filename in [".api_keys.json", "profiles.json"]:
                path = os.path.join(self._config_dir, filename)
                self._file_stats[filename] = self._get_file_stat(path)
            self._last_reload_time = time.time()
        except Exception as e:
            # Preserve old cache data so auth continues working with stale data
            logger.warning("[AUTH] Failed to reload databases: %s - continuing with cached data", e)
            # Force immediate reload on next call (will retry until success)
            self._last_reload_time = 0.0

    def authenticate(self, api_key: str) -> Optional[Dict]:
        """Authenticate an API key and return user info with role + permissions.

        Returns dict with keys: email, role, allowed_tools (list or None=all),
                                allowed_models (list or None=all), max_connections, etc.
        Returns None if auth fails.

        MODEL RESOLUTION:
          - Uses profile.allowed_models_folders (disk paths) resolved to .gguf filenames
        """
        if self._should_reload():
            self._reload_databases()

        if not api_key:
            return None

        # Find user by API key
        user_entry = None
        for entry in self._api_keys_db:
            if (isinstance(entry, dict)
                and _keys_equal(entry.get("key"), api_key)
                and entry.get("is_active", True)):
                user_entry = entry
                break

        if not user_entry:
            logger.warning("[AUTH] Invalid or inactive API key provided")
            return None

        role = user_entry.get("role", "user")

        # Look up profile for this role
        profile = self._profiles_db.get(role)
        if not profile:
            logger.warning(f"[AUTH] No valid profile found for role '{role}' - rejecting")
            return None  # STRICT: no profile = no access

        # Resolve allowed_models_folders to actual .gguf filenames (llama.cpp)
        folders = profile.get("allowed_models_folders")
        resolved_models = resolve_folders_to_models(folders) if folders is not None else None

        # Build user info with merged permissions
        user_info = {
            "email": user_entry.get("email", "unknown"),
            "role": role,
            "allowed_tools": profile.get("allowed_tools"),  # None = all tools allowed
            "allowed_models": resolved_models,  # None = all models allowed
            "max_connections": profile.get("max_connections", 1),
            "max_rate_limit": profile.get("max_rate_limit", PROFILE_DEFAULT_RATE_LIMIT),
            # python_exec blocked-libs SET FILE for this profile (e.g. "blocked_libs_set01.json").
# None = no per-profile set -> built-in default blocklist applies on the tool side.
# The file CONTENT is loaded in direct_ws.tools_request_handler._handle_tools_request() and shipped to the CLIENT
            # so python_exec enforces exactly this profile's constraints where it executes.
            "python_exec_blocked_libs": profile.get("python_exec_blocked_libs"),
        }

        logger.info(
            f"[AUTH] User authenticated: role={role}, "
            f"models={'all' if resolved_models is None else len(resolved_models)}"
        )
        return user_info


    # --- Permission Filtering (delegated to model_resolver) ---

    @staticmethod
    def get_allowed_models(user_info: Dict, all_models: List[Dict]) -> List[Dict]:
        """Filter available models based on user's profile."""
        from .model_resolver import filter_allowed_models
        return filter_allowed_models(user_info, all_models)

    @staticmethod
    def get_allowed_tools(user_info: Dict, requested_tools: List[str]) -> List[str]:
        """Filter requested tools based on user's profile."""
        from .model_resolver import filter_allowed_tools
        return filter_allowed_tools(user_info, requested_tools)


# Import helper functions used by AuthGateway class
from .profile_loader import (
    _resolve_config_dir,
    load_api_keys_db,
    load_blocked_libs_file,
    load_profiles_db,
)
from .model_resolver import resolve_folders_to_models
