"""
COOLEMS Configuration Package (SERVER) — single source of truth.

This package centralizes all SERVER configuration:

  - config.py       : Global tunable parameters (context, timeouts, providers, etc.)
  - .api_keys.json  : API key storage with per-user overrides (list of key entries)
  - profiles.json   : Role-based permission profiles

Import everything from this package:

    from config import MODEL_NAME, PROVIDER_DEFAULT_TIMEOUT, get_api_url

NOTE on CLIENT: the CLIENT has its own separate config package under CLIENT/config/.
Values that must be identical on both sides (WEB_RELAY_* connection constants) are
kept in sync manually; values consumed by shipped tools are delivered at runtime via
tools_response 'config_constants' (see app/providers/coolems/tool_scanner/config_constants.py -- package split 2026-09-07).
"""

from .config import (
    # Context / Token Limits
    CONTEXT_WINDOW_TOKENS,

    # Provider Selection
    PROVIDER,
    SERVER_BACKEND,
    COOLEMS_SERVER_HOST,

    # Coolems Web Relay / Direct WS Configuration (shared with CLIENT + relay)
    PROTOCOL_VERSION,   # WS protocol version -- echoed in auth_ok; enforced by tests/test_protocol_sync_20260820.py
    USE_WEB_SERVER,
    WEB_RELAY_MAX_MESSAGE_SIZE,
    WEB_RELAY_HEARTBEAT_INTERVAL,
    WEB_RELAY_HEARTBEAT_TIMEOUT,
    WS_HANDSHAKE_TIMEOUT,
    WEB_RELAY_DEFAULT_PORT,
    WEB_RELAY_VERIFY_SSL_CERTS,
    WEB_RELAY_CERT_PIN_FINGERPRINT,   # (2026-09-01 S3) optional cert pin for the relay link
    COOLEMS_RECONNECT_MIN_DELAY,
    COOLEMS_RECONNECT_MAX_DELAY,

    # Direct WebSocket mode config
    COOLEMS_DIRECT_WS_PORT,
    COOLEMS_DIRECT_WS_CERT_PATH,
    COOLEMS_DIRECT_WS_KEY_PATH,

    # Model / API URLs
    LLAMA_URL,
    MODEL_NAME,

    # Chrome CDP (Browser Debugging)
    CHROME_CDP_PORT,

    # Provider Defaults
    PROVIDER_DEFAULT_TIMEOUT,
    PROFILE_DEFAULT_RATE_LIMIT,

    # Provider Streaming Buffer
    STREAM_BUFFER_SIZE_CHARS,
    STREAM_BUFFER_SIZE_MSGS,
    STREAM_BUFFER_TIMEOUT_SEC,

    # Provider Health
    HEALTH_CHECK_TIMEOUT,
    HEALTH_CHECK_INTERVAL_SEC,
    PROVIDER_STARTUP_TIMEOUT,

    # --- Chat Request Queue (2026-09-08 multi-chat) ---
    QUEUE_MAX_PENDING,
    QUEUE_STATS_LOG_INTERVAL_SEC,

    # --- Llama Server Configuration ---
    LLAMA_SERVER_THREADS,
    LLAMA_SERVER_GPU_LAYERS,
    LLAMA_SERVER_PORT,

    # Llama server instance list (hand-editable config/llama_servers.json, 2026-09-08)
    LLAMA_SERVERS_FILE,
    LlamaServerInstance,
    get_llama_server_instances,
    LLAMA_OCR_SERVER_PORT,
    LLAMA_OCR_CTX_SIZE,
    LLAMA_OCR_UNLOAD_AFTER_USE,
    LLAMA_SERVER_STARTUP_WAIT_SEC,
    LLAMA_SERVER_MODEL_LOAD_BUDGET_SEC,

    # Model state cache (AUTO-SWITCH optimization)
    LLAMA_MODEL_STATE_TTL_SEC,

    # Stream stall watchdog (llama self-heal)
    LLAMA_STREAM_STALL_TIMEOUT_SEC,
    LLAMA_AUTO_RESTART_ON_STALL,

    # KV Cache Optimization
    LLAMA_SERVER_CACHE_TYPE_K,
    LLAMA_SERVER_CACHE_TYPE_V,
    LLAMA_SERVER_FLASH_ATTN,
    LLAMA_SERVER_PARALLEL,
    LLAMA_SERVER_CACHE_RAM,

    # Llama Server MTP (Multi-Token Prediction) Support
    LLAMA_SERVER_MTP_ENABLED,
    LLAMA_SERVER_SPEC_TYPE,
    LLAMA_SERVER_SPEC_DRAFT_N_MAX,
    LLAMA_SERVER_SPEC_DRAFT_N_MIN,
    LLAMA_SERVER_SPEC_DRAFT_P_SPLIT,
    LLAMA_SERVER_SPEC_DRAFT_P_MIN,

    # File Loading Flags
    LLAMA_SERVER_NO_MMAP,
    LLAMA_SERVER_NO_WARMUP,
    LLAMA_SERVER_JINJA,
    LLAMA_SERVER_FIT,

    # Generation Defaults
    LLAMA_SERVER_TEMP,
    LLAMA_SERVER_REPEAT_PENALTY,
    LLAMA_SERVER_MAX_NEW_TOKENS,
    LLAMA_MIN_SAFE_MAX_TOKENS,

    # Reasoning / Thinking Mode
    LLAMA_SERVER_REASONING,
    LLAMA_SERVER_REASONING_BUDGET,
    LLAMA_SERVER_PRESERVE_THINKING,

    # Timeouts (General)
    OCR_TIMEOUT,

    # OCR
    OCR_MAX_TOKENS,

    # Web Search
    WEB_SEARCH_DEFAULT_MAX_RESULTS,
    WEB_SEARCH_DDGS_DEFAULT_RESULTS,
    WEB_SEARCH_WIKIPEDIA_TIMEOUT,
    WEB_SEARCH_DDGS_TIMEOUT,
    WEB_SEARCH_BING_TIMEOUT,
    WEB_SEARCH_SEARXNG_TIMEOUT,
    WEB_SEARCH_RESULT_SNIPPET_CHARS,
    WEB_SEARCH_SEARXNG_INSTANCES,

    # File Handling (python_exec limits shipped to CLIENT tools via config_constants;
    # the file_tools size caps live in tools/utils.py — not here)
    FILE_EXEC_OUTPUT_TRUNCATE_CHARS,
    FILE_EXEC_ERROR_TRUNCATE_CHARS,
    FILE_EXEC_TIMEOUT,

    # Image Generation (shipped to CLIENT tools via config_constants).
    # IMAGE_GEN_TIER_OVERRIDE re-exported 2026-09-22: generate_image.py imports it
    # from 'config' at module level (server local-dev path) but the package __init__
    # only exported IMAGE_SUBPROCESS_TIMEOUT -- every server-side import of the tool
    # died with ImportError until this line existed.
    IMAGE_GEN_TIER_OVERRIDE,
    IMAGE_SUBPROCESS_TIMEOUT,

    # Derived helpers
    get_api_url,
    get_provider_name,
)


# Path helpers for config data files

import os as _os

_CONFIG_DIR = _os.path.dirname(_os.path.abspath(__file__))

API_KEYS_FILE = _os.path.join(_CONFIG_DIR, ".api_keys.json")
PROFILES_FILE = _os.path.join(_CONFIG_DIR, "profiles.json")
