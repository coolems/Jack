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
            self._validate_profiles()
        except Exception as e:
            # Preserve old cache data so auth continues working with stale data
            logger.warning("[AUTH] Failed to reload databases: %s - continuing with cached data", e)
            # Force immediate reload on next call (will retry until success)
            self._last_reload_time = 0.0

    def _validate_profiles(self) -> None:
        """Log one line per role: slot cap + allowed models (boot + every reload).

        Missing max_parallel_slots is a CONFIG ERROR: the role cannot authenticate
        (no fallback). The server still starts so the operator can fix profiles.json
        and the hot reload picks it up without a restart.
        """
        for role, profile in self._profiles_db.items():
            if not isinstance(profile, dict):
                logger.error("[AUTH] Profile '%s' is not an object - role unusable", role)
                continue
            slots = profile.get("max_parallel_slots")
            models = profile.get("allowed_models")
            if isinstance(slots, int) and not isinstance(slots, bool) and slots >= 1:
                shown = "all" if models is None else (
                    f"{len(models)} model(s)" if isinstance(models, list) else "INVALID"
                )
                logger.info("[AUTH] role=%-12s max_parallel_slots=%d allowed_models=%s",
                            role, slots, shown)
            else:
                logger.error(
                    "[AUTH] role=%-12s has NO valid max_parallel_slots (got %r) -> "
                    "authentication for this role will be DENIED until profiles.json is fixed",
                    role, slots,
                )

    def authenticate(self, api_key: str) -> Optional[Dict]:
        """Authenticate an API key and return user info with role + permissions.

        Returns dict with keys: email, role, allowed_tools (list or None=all),
                                allowed_models (list of filenames or None=all),
                                max_parallel_slots (REQUIRED int >= 1), max_connections, etc.
        Returns None if auth fails (including a profile with no max_parallel_slots).

        MODEL RESOLUTION:
          - Model identity is the GGUF FILENAME; profiles.json lists allowed filenames
            explicitly (2026-10 fleet - folders are no longer used).
        """
        if self._should_reload():
            self._reload_databases()

        # (2026-10-05 DIAGNOSTICS): distinguish "no key sent at all" from "wrong key".
        # The old single warning made credential-drift bugs (e.g. a stale
        # COOLEMS_CLIENT_API_KEY after a key rotation) nearly impossible to diagnose -
        # the log looked identical for an empty handshake and a rejected real key.
        if not api_key:
            logger.warning("[AUTH] No API key provided in auth request")
            return None

        # Find user by API key
        user_entry = None
        inactive_match = False
        for entry in self._api_keys_db:
            if isinstance(entry, dict) and _keys_equal(entry.get("key"), api_key):
                if entry.get("is_active", True):
                    user_entry = entry
                    break
                inactive_match = True

        if not user_entry:
            # (2026-10-05 DIAGNOSTICS): name the exact failure mode - a key that EXISTS
            # but is deactivated reads differently from one that matches no entry.
            if inactive_match:
                logger.warning("[AUTH] API key exists but its entry is marked inactive")
            else:
                logger.warning("[AUTH] Invalid or inactive API key provided")
            return None

        role = user_entry.get("role", "user")

        # Look up profile for this role
        profile = self._profiles_db.get(role)
        if not profile or not isinstance(profile, dict):
            logger.warning(f"[AUTH] No valid profile found for role '{role}' - rejecting")
            return None  # STRICT: no profile = no access

        # (2026-10 fleet) Model authorization is by FILENAME only; there are no folders.
        # None = all models (admin), otherwise an explicit list of GGUF filenames.
        allowed_models = profile.get("allowed_models")
        if allowed_models is not None:
            if (not isinstance(allowed_models, list)
                    or not all(isinstance(m, str) and m.strip() for m in allowed_models)):
                logger.error(
                    "[AUTH] Profile '%s' has an invalid 'allowed_models' (expected a list "
                    "of GGUF filenames, or null for all) - rejecting authentication", role,
                )
                return None
            allowed_models = [m.strip() for m in allowed_models]

        # (2026-10 fleet) max_parallel_slots is REQUIRED. No fallback of any kind:
        # a profile without a valid value cannot authenticate (the operator must fix
        # profiles.json; hot reload picks it up without a restart).
        slots = profile.get("max_parallel_slots")
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            logger.error(
                "[AUTH] Profile '%s' has no valid 'max_parallel_slots' (int >= 1, got %r) - "
                "DENYING authentication for role '%s'. Add \"max_parallel_slots\": N to "
                "config/profiles.json.", role, slots, role,
            )
            return None

        # Build user info with merged permissions
        user_info = {
            "email": user_entry.get("email", "unknown"),
            "role": role,
            "allowed_tools": profile.get("allowed_tools"),  # None = all tools allowed
            "allowed_models": allowed_models,  # None = all models allowed (filenames)
            "max_parallel_slots": slots,       # REQUIRED: parallel generations for this user
            "max_connections": profile.get("max_connections", 1),
            "max_rate_limit": profile.get("max_rate_limit", PROFILE_DEFAULT_RATE_LIMIT),
            # python_exec blocked-libs SET FILE for this profile (e.g. "blocked_libs_set01.json").
# None = no per-profile set -> built-in default blocklist applies on the tool side.
# The file CONTENT is loaded in direct_ws.tools_request_handler._handle_tools_request() and shipped to the CLIENT
            # so python_exec enforces exactly this profile's constraints where it executes.
            "python_exec_blocked_libs": profile.get("python_exec_blocked_libs"),
        }

        logger.info(
            f"[AUTH] User authenticated: role={role}, slots={slots}, "
            f"models={'all' if allowed_models is None else len(allowed_models)}"
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
