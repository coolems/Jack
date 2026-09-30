"""
COOLEMS Client Configuration Package.

Client-only config - NO server secrets (no API keys, no auth profiles).
For server config with secrets, see root config/config.py.

Import everything from this package:
    from config import MODEL_NAME, PROVIDER_DEFAULT_TIMEOUT, get_api_url
"""

from .config import (
    # --- Context / Token Limits ---
    CONTEXT_WINDOW_TOKENS,
    HISTORY_CONTEXT_FRACTION,
    CHARS_PER_TOKEN,
    SYSTEM_PROMPT_TOKENS_ESTIMATE,
    IMAGE_TOKENS_PER_512PX,

    # --- Cross-Task Memory ---
    CROSS_TASK_MEMORY_CONVERSATIONS,
    CROSS_TASK_MEMORY_CONTEXT_FRACTION,

    # --- Provider / Model (Client Side) ---
    PROVIDER,

    # --- Default Role ---
    DEFAULT_ROLE_NO_KEY,

    # --- Coolems Client Connection Settings ---
    COOLEMS_CLIENT_SERVER_ADDRESS,
    USE_WEB_SERVER,
    get_connection_mode,        # (2026-09-23) 'direct' | 'web_relay' from settings.json (default direct)
    WEB_RELAY_UI_ENABLED,       # (2026-09-23) show/hide the Internet Web Relay option in UI + force direct when off
    get_web_relay_address,      # (2026-09-23) relay host[:port] from settings.json (or None)
    WEB_RELAY_HEARTBEAT_INTERVAL,
    WEB_RELAY_HEARTBEAT_TIMEOUT,
    WS_HANDSHAKE_TIMEOUT,
    WEB_RELAY_MAX_MESSAGE_SIZE,
    WEB_RELAY_VERIFY_SSL_CERTS,
    WEB_RELAY_CERT_PIN_FINGERPRINT,   # (2026-09-01 S3) optional cert pin for the relay link
    COOLEMS_DIRECT_VERIFY_SSL_CERTS,
    COOLEMS_SERVER_CERT_PATH,
    COOLEMS_RECONNECT_MIN_DELAY,
    COOLEMS_RECONNECT_MAX_DELAY,
    COOLEMS_DIRECT_WS_PORT,
    WEB_RELAY_DEFAULT_PORT,

    # --- Protocol Versioning (2026-08-20) ---
    PROTOCOL_VERSION,
    CHAT_BACKGROUND_FRAME_BUFFER,   # (2026-09-08 multi-chat) background frame buffer per chat
    CLIENT_MAX_OPEN_CHAT_WS,        # (2026-09-08 multi-chat) UI LRU cap on open per-chat sockets

    MODEL_NAME,

    # --- Model Switch Timing (CLIENT side) ---
    MODEL_SWITCH_PHASE1_TIMEOUT,
    MODEL_SWITCH_POLL_INTERVAL,
    MODEL_SWITCH_PROGRESS_LOG_SEC,
    MODEL_SWITCH_MAX_WAIT_SEC,
    MODEL_SWITCH_CLIENT_BUDGET_SEC,



    # --- Bootstrap wait (CLIENT startup -> SERVER delivery) ---
    BOOTSTRAP_MAX_WAIT_SEC,
    BOOTSTRAP_RETRY_INITIAL_DELAY,
    BOOTSTRAP_RETRY_MAX_DELAY,
    BOOTSTRAP_PROGRESS_LOG_SEC,
    BOOTSTRAP_ATTEMPT_TIMEOUT,
    # --- Provider Defaults ---
    PROVIDER_DEFAULT_TIMEOUT,
    PROVIDER_DEFAULT_TEMPERATURE,

    # --- Agentic Loop ---
    AGENTIC_MAX_TOOL_ITERATIONS,
    AGENTIC_MAX_THINKING_ITERATIONS,
    AGENTIC_TOKEN_WARNING_THRESHOLD,
    AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS,
    AGENTIC_ITERATION_TIMEOUT_SEC,
    AGENTIC_TOOL_OUTPUT_MAX_CHARS,

    # --- Database ---
    DB_PATH,


    # --- File Handling ---
    FILE_MAX_SIZE_BYTES,
    FILE_MAX_TEXT_CONTENT_BYTES,
    FILE_MAX_WRITE_BYTES,
    FILE_EXEC_OUTPUT_TRUNCATE_CHARS,
    FILE_EXEC_ERROR_TRUNCATE_CHARS,
    FILE_EXEC_TIMEOUT,

    # --- Derived Helpers ---
    get_api_url,
    get_provider_name,
    max_history_tokens,
    estimate_tokens,
    # (2026-08-20) live readers -- prefer these over bare name imports for values the
    # SERVER updates at runtime (context window, model).
    get_context_window,
    get_model_name,
    _load_settings,
    _save_settings,
    _resolve_client_server_address,
    get_server_address_list,
    # (2026-09-01 sticky failover) known-good server memory
    get_good_server,
    _remember_good_server,
)

# --- Search engines (search_engines.json - single source of truth) ---
from .config import (  # noqa: F401,E501  (re-exported for 'from config import ...')
    load_search_engines,
    save_search_engines,
)
