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

    # --- Local CLIENT Web UI Port (default for entry/cli_parser.py --port;
    # mirror of SERVER config/config.py CLIENT_UI_PORT - change both together) ---
    CLIENT_UI_PORT,

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
    CHAT_STEER_QUEUE_MAX,           # (2026-10-23 live steering) max queued mid-run user messages

    # --- Runtime notice lane (2026-12 subagents + background jobs + questions) ---
    # A notice is the RUNTIME speaking (child settled, job settled, user answered a
    # question the AI asked). Same injection boundaries as steering, different header,
    # different persistence flag (messages.is_notice), and it never defers a completion.
    NOTICE_QUEUE_MAX,
    NOTICE_MAX_CONSECUTIVE_WAKES,
    NOTICE_QUIET_WAKE,
    NOTICE_INLINE_BYTES,

    # --- Subagents (2026-12) ---
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_MAX_CONCURRENT_PER_CONV,
    SUBAGENT_NOTICE_INLINE_BYTES,
    SUBAGENT_TIMEOUT_SEC,

    # --- Background jobs (2026-12) ---
    JOB_LIVE_RETAIN_BYTES,
    JOB_SETTLED_RETAIN_BYTES,
    JOB_MAX_CONCURRENT_PER_CONV,
    JOB_WAIT_TIMEOUT_SEC,
    JOB_MAX_WAIT_TIMEOUT_SEC,
    JOB_INLINE_OUTPUT_BYTES,
    JOB_PUMP_POLL_MS,
    JOB_SPILL_KEEP_FILES,

    # --- Parallel tool dispatch (2026-12) ---
    TOOL_MAX_PARALLEL,

    # --- ask_user_question (2026-12) ---
    QUESTION_TIMEOUT_DEFAULT_SEC,
    QUESTION_MAX_OPTIONS,

    # --- MCP servers (2026-12) ---
    # Servers themselves are declared in CLIENT/config/mcp_servers.json and stay disabled
    # until the human enables them; these bound what a server's payload may reach.
    MCP_TEXT_PREVIEW_CHARS,
    MCP_INSTRUCTIONS_MAX_CHARS,
    MCP_TIMEOUT_DEFAULT_SEC,
    MCP_TIMEOUT_MAX_SEC,
    MCP_SPILL_KEEP_FILES,

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
    AGENTIC_TOKEN_WARNING_THRESHOLD,
    AGENTIC_CONTEXT_GUARDIAN_WINDOW_FRACTION,
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
    # (2026-10-02) Runtime tab - python_exec dialog behavior (live reader)
    get_runtime_settings,
    # (2026-11-07) consent card kinds + their settings.json key pairs. Exported so the
    # settings endpoints and the UI stay driven by ONE list (CONSENT_KINDS) instead of
    # per-file hardcoded copies. 'desktop' = Computer Use (tools/computer_use/*).
    CONSENT_KINDS,
    CONSENT_SETTING_KEYS,
)

# --- Search engines (search_engines.json - single source of truth) ---
from .config import (  # noqa: F401,E501  (re-exported for 'from config import ...')
    load_search_engines,
    save_search_engines,
)
