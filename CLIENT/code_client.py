"""
COOLEMS CLIENT - Main Entry Point (Client-Side Only)

Thin wrapper for client-side execution. No server code, no auth keys.

CRITICAL: ONLY supports 'coolems_client' provider (WebSocket connection to SERVER).

All LLM requests MUST go through the SERVER relay. Direct LLM access is BLOCKED.


HTTPS Support:
  Uses self-signed certificates from CLIENT/certs/ when available.
  Falls back to HTTP if certs are missing (standalone distribution mode).


Auto Port Kill:
  Automatically kills any process blocking the target port before starting uvicorn.


Usage:
  python code_client.py                    # Start UI server connecting to SERVER via WebSocket
  python code_client.py provider=coolems_client  # Explicit coolems_client (default)

IMPORTANT: To start a local UI with direct LLM access, run code.py in the SERVER root folder.
"""

import sys
import os
import asyncio
import atexit

# Ensure CLIENT directory is in path for imports AND set as working directory
CLIENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CLIENT_DIR not in sys.path:
    sys.path.insert(0, CLIENT_DIR)

# CRITICAL: Change working directory to CLIENT/ so all relative paths work correctly
os.chdir(CLIENT_DIR)

# If running from within server root (dev mode), ensure we use CLIENT config first
SERVER_ROOT = os.path.dirname(CLIENT_DIR)
sys.path = [p for p in sys.path if not os.path.abspath(p) == SERVER_ROOT]

# Expose the CLIENT directory to everything running in this process (self-unpacking
# tool bootstrap included): content-generating tools unfold their heavy runtimes under
# <CLIENT>/tools/runtimes/<name> - INTERNAL to the client, never into the user's working
# folder. setdefault keeps it overridable for tests / custom deployments.
os.environ.setdefault("COOLEMS_CLIENT_ROOT", CLIENT_DIR)


from config import (
    DB_PATH as CONFIG_DB_PATH,
    MODEL_NAME as CONFIG_MODEL_NAME,
    PROVIDER as CONFIG_PROVIDER,
    get_api_url as CONFIG_GET_API_URL,
    PROVIDER_DEFAULT_TIMEOUT as CONFIG_PROVIDER_DEFAULT_TIMEOUT,
)

# FIX: Import utils.history_manager BEFORE tools to prevent shadowing
import utils.history_manager

from app.browser import WebInteract
from app.logging_config import setup_logging
from app.database import init_db, migrate_conversation_colors
from app.endpoints import create_app
from app.initialization_client import init_provider_and_app, resolve_provider_and_url

# Import modular entry components
from entry.cli_parser import parse_args
from entry.port_manager import ensure_port_free
from entry.ssl_config import detect_ssl_config
from entry.sensitive_filter import (
    SensitiveStderr,
    create_uvicorn_log_config,
)

# == Logging Setup ==
logger, ws_logger, provider_logger, agent_logger, db_logger = setup_logging()

logger.info("=" * 60)
logger.info("COOLEMS CLIENT Starting Up")
logger.info("=" * 60)

# Configuration (static values that do not depend on provider override)
API_URL = CONFIG_GET_API_URL()
DB_PATH = CONFIG_DB_PATH
MODEL_NAME = CONFIG_MODEL_NAME
PROVIDER_DEFAULT_TIMEOUT = CONFIG_PROVIDER_DEFAULT_TIMEOUT

logger.info(f"Config Provider: {CONFIG_PROVIDER} at {API_URL}")
logger.info(f"Timeout: {PROVIDER_DEFAULT_TIMEOUT}s")

# ===== Config Bootstrap (2026-08-23) =====
# Create missing CLIENT config data files from their shipped example templates so a
# fresh checkout never fails at startup: .api_client_keys.json, settings.json,
# .working_root.json. Existing files are NEVER touched.
from config.bootstrap import ensure_client_config_files, ensure_database_file
_created_configs = ensure_client_config_files()
if _created_configs:
    logger.info("Client bootstrap created missing file(s): " + ", ".join(_created_configs))

# ===== Database Setup =====
ensure_database_file(DB_PATH)  # parent dir exists (file itself is created by init_db)
init_db(DB_PATH)
migrate_conversation_colors(DB_PATH)

# ===== Provider-Dependent Objects (initialized in main()) =====
provider = None
tool_orchestrator = None
agent = None  # Initialized AFTER provider setup (DNA comes from SERVER via WebSocket)
app = None

# ===== Global Stop Events =====
stop_events = {}

# ===== Cleanup on Exit =====
atexit.register(WebInteract.cleanup)


if __name__ == "__main__":
    import uvicorn

    # 1. Parse arguments FIRST
    port, provider_arg = parse_args()

    # 2. Determine final provider name and URL (MUST be coolems_client)
    try:
        final_provider_name, final_api_url = resolve_provider_and_url(cli_provider=provider_arg)
    except ValueError as e:
        logger.error(f"Provider resolution failed: {e}")
        sys.exit(1)

    # 3. Initialize provider, tool orchestrator, and agent (DNA delivered from SERVER here).
    # bootstrap_framework() WAITS for the SERVER while it boots / loads its model (see
    # BOOTSTRAP_MAX_WAIT_SEC in config/config.py) - so "server still starting" is NOT a crash.
    # A failure that survives the wait budget exits cleanly with ONE actionable message,
    # not a wall of traceback (2026-08-26).
    #
    # (2026-09 threadless refactor): init_provider_and_app() is async now - the ENTIRE
    # boot sequence (framework fetch + provider build + agent init + health check) runs in
    # ONE top-level asyncio.run(_boot()) below, on a single event loop. This is the only
    # asyncio.run left in the CLIENT: it sits at the true program entry point, exactly like
    # code.py does for the SERVER. uvicorn then starts its own loop afterwards; nothing
    # from boot outlives that loop (no lingering tasks/sockets).
    async def _boot():
        return await init_provider_and_app(final_provider_name, final_api_url)

    try:
        provider, tool_orchestrator, agent, MODEL_NAME, DB_PATH = asyncio.run(_boot())
    except KeyboardInterrupt:
        logger.info("Client startup cancelled by user (Ctrl+C)")
        sys.exit(130)
    except RuntimeError as e:
        # The message is already actionable and names the root cause (e.g. SERVER never
        # came up, stale SERVER build, agent init failure). No traceback dump.
        logger.error(f"[FATAL] {e}")
        sys.exit(1)

    # Agent DNA is now available (delivered from SERVER via WebSocket)
    logger.info(f"{agent.get_name()} DNA system initialized from SERVER")

    # Register goodbye message with agent name now that it's available
    atexit.register(lambda: logger.info(f"{agent.get_name()} says goodbye. Until next time!"))

    # ===== HTTPS Configuration (detected BEFORE create_app so the CORS scheme and
    # startup log reflect reality) =====
    has_ssl, ssl_certfile, ssl_keyfile = detect_ssl_config(CLIENT_DIR, SERVER_ROOT)

    if has_ssl:
        protocol = "https"
        logger.info("HTTPS enabled (SSL certificates found)")
    else:
        protocol = "http"
        logger.warning("No SSL certificates found in CLIENT/certs/ - running HTTP only")

    # Create FastAPI app with the correct provider (coolems_client only)
    app = create_app(
        db_path=DB_PATH,
        provider=provider,
        model_name=MODEL_NAME,
        api_timeout=PROVIDER_DEFAULT_TIMEOUT,
        agent=agent,
        tool_orchestrator=tool_orchestrator,
        stop_events=stop_events,
        server_port=port,
        use_https=has_ssl,
    )
    # ===== Bind Address (SECURITY 2026-08-25) =====
    # Default: loopback only (127.0.0.1). The CLIENT UI is a local tool; binding all
    # interfaces exposes the file API to every LAN host. Set COOLEMS_CLIENT_HOST=0.0.0.0
    # explicitly ONLY if you deliberately want LAN access with keys configured.
    import ipaddress as _ipaddr
    bind_host = os.environ.get("COOLEMS_CLIENT_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        if not (bind_host in ("0.0.0.0", "::") or _ipaddr.ip_address(bind_host).is_loopback):
            logger.warning(f"COOLEMS_CLIENT_HOST={bind_host!r} is neither loopback nor 0.0.0.0 - binding it anyway (explicit operator choice)")
    except ValueError:
        pass
    if bind_host in ("0.0.0.0", "::"):
        from app.keys import get_api_keys as _gak
        if not _gak():
            logger.error("Refusing to bind 0.0.0.0 with no API key available (would expose unauthenticated access).")
            logger.error("Set the COOLEMS_CLIENT_API_KEY environment variable (or complete setup in the UI first) - or unset COOLEMS_CLIENT_HOST.")
            sys.exit(1)
        logger.warning(f"SECURITY: CLIENT bound to {bind_host} - ALL interfaces. Anyone on the network can reach port {port}.")

    logger.info(f"Starting CLIENT server on {bind_host}:{port} ({protocol.upper()})")

    # ===== Ensure Port is Free (kill blocking processes) =====
    if not ensure_port_free(port):
        sys.exit(1)

    # ===== Stderr Filter =====
    # CRITICAL: uvicorn/websockets library prints WebSocket connection logs DIRECTLY to stderr
    # using print() or sys.stderr.write(). These BYPASS Python logging system entirely.
    original_stderr = sys.stderr
    sys.stderr = SensitiveStderr(original_stderr)


    # ===== Uvicorn Config =====
    uvicorn_log_config = create_uvicorn_log_config()

    uvicorn_kwargs = {
        "app": app,
        "host": bind_host,
        "port": port,
        "ws_per_message_deflate": False,
        "ws_max_size": 96 * 1024 * 1024,
        "limit_concurrency": 100,
        "backlog": 2048,
        "access_log": False,
        "log_config": uvicorn_log_config,
    }

    if has_ssl:
        uvicorn_kwargs["ssl_certfile"] = ssl_certfile
        uvicorn_kwargs["ssl_keyfile"] = ssl_keyfile

    config_uvicorn = uvicorn.Config(**uvicorn_kwargs)
    server = uvicorn.Server(config_uvicorn)

    try:
        server.run()
        sys.exit(0)
    except KeyboardInterrupt:
        logger.info("Client stopped by user (KeyboardInterrupt)")
        sys.exit(130)
    except SystemExit as e:
        logger.info(f"Client received SystemExit with code: {e.code}")
        sys.exit(e.code)
    except Exception as e:
        logger.exception(f"Client crashed with unhandled exception: {e}")
        sys.exit(-1)
