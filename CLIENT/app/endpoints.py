"""
COOLEMS API Endpoints - Thin orchestrator that assembles all route modules.
WebSocket is handled separately in websocket.py.

Route modules:
  - conversations.py : /api/conversations/* (CRUD, messages, truncate, reset)
  - files.py         : /api/upload, /api/tree, /api/download, /api/file-content, /api/open
  - agent.py         : /api/models, /api/status, /api/agent/*, /api/stop/*, /api/my-permissions
  - downloads.py     : /api/setup/status?tool=... (live venv/pip/model setup progress)

UPDATED (2025-06-25):
    - Added init_db_manager() call in startup for safe DB pooling with WAL mode

UPDATED: Passes API key through to WebSocket handler for role-based tool filtering.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import logging
import time
logger = logging.getLogger("COOLEMS")


# FIX (2026-08-17): anchor the log dir to this module's location (app/ -> <CLIENT>/logs)
# instead of CWD-relative "logs" -- a process started from another folder would otherwise
# create stray logs/ directories outside the project tree.
_LOGS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))

# FIX (2026-08-17): anchor the CLIENT root to this module location (app/ -> <CLIENT>) so UI files
# never resolve against CWD -- a process started from another folder would otherwise 404 on / and /static.
_CLIENT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


# ===== Module-level cleanup functions =====
# (Moved here so they are visible to startup_event inside create_app())



def create_app(db_path: str, provider, model_name: str, api_timeout: int,
               agent, tool_orchestrator, stop_events, server_port=8000, use_https=False):
    """Create and configure the FastAPI application with all endpoints."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse, JSONResponse

    from app.auth import APIMiddleware
    from app.keys import get_api_keys

    from app.routers.conversations import create_conversations_router
    from app.routers.files import create_files_router
    from app.routers.agent import create_agent_router
    from app.routers.downloads import create_downloads_router
    from app.websocket import create_websocket_handler

    # ===== Initialize database manager (WAL mode, safe defaults) =====
    from app.db_manager import init_db_manager
    init_db_manager(db_path)

    # ===== (2026-09-08 multi-chat) ChatBus: per-chat background sessions =====
    # One channel per conversation; generation runs as a background task, so switching
    # chats in the browser never kills another chat's session. Created ONCE here and
    # shared by the WebSocket handler factory + (via stop_events) the /api/stop route.
    from app.chat_bus import ChatBus, set_chat_bus

    chat_bus = ChatBus(
        db_path=db_path,
        provider=provider,
        model_name=model_name,
        api_timeout=api_timeout,
        agent=agent,
        tool_orchestrator=tool_orchestrator,
        stop_events=stop_events,  # legacy dict stays the source of truth for /api/stop
    )
    set_chat_bus(chat_bus)

    app = FastAPI(title="COOLEMS API", version="5.0.0")

    # Create directories (anchored to <CLIENT>/logs -- never CWD/working_root)
    os.makedirs(_LOGS_DIR, exist_ok=True)

    # ===== CORS (SECURITY 2026-08-31) =====
    # The UI is served BY THIS SAME SERVER (/ and /static/), so normal page traffic is
    # same-origin and needs no CORS at all. This block only exists for the narrow case
    # where a separate local tool (e.g. an editor preview on another port) calls /api/*.
    #
    # Why NOT allow_origins=["*"] + allow_credentials=True:
    #   - Browsers REJECT wildcard + credentials, so it silently does nothing useful;
    #     worse, it advertises "any origin may call us with credentials" intent.
    #   - Auth here is header-based (X-API-Key / Authorization), never cookies, so
    #     allow_credentials must be False.
    #
    # Debugging: a request from a disallowed origin gets HTTP 400
    # "Disallowed CORS origin", and the effective allowed list is logged at startup.
    _scheme = "https" if use_https else "http"
    _cors_origins = [f"{_scheme}://localhost:{server_port}", f"{_scheme}://127.0.0.1:{server_port}"]
    for extra in os.environ.get("COOLEMS_CORS_ORIGINS", "").split(","):
        extra = extra.strip()
        if extra and extra not in _cors_origins:
            _cors_origins.append(extra)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,          # explicit list - no wildcards
        allow_credentials=False,              # header auth only; cookies are never used
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],   # exactly what routers use
        allow_headers=["X-API-Key", "X-User-Email", "Authorization", "Content-Type"],  # exactly what the UI sends
    )
    
    # ===== API Key Authentication Middleware =====
    # Protects all /api/*, /ws/* AND /files/* endpoints.
    # Only the UI shell is public: / and /static/. (SECURITY 2026-08-25: /files/
    # was removed from the public list - file content now requires authentication.)

    # FIX (2026-08-26): APIMiddleware was imported above but NEVER registered, so
    # every HTTP request reached the routers WITHOUT auth state -> agent.py fell
    # back to role 'unknown' ("Role 'unknown': N model(s) available") and /api/models
    # returned an unfiltered list. Registered AFTER CORS on purpose: Starlette runs
    # middleware in REVERSE registration order, so APIMiddleware becomes the OUTERMOST
    # layer (auth is evaluated before CORS handling), exactly as designed.

    app.add_middleware(APIMiddleware)

    # Static files
    app.mount("/static", StaticFiles(directory=os.path.join(_CLIENT_ROOT, "static"), check_dir=False), name="static")
    
    # ===== Root =====
    @app.get("/")
    async def root():
        return FileResponse(os.path.join(_CLIENT_ROOT, "index.html"))

    # ===== STARTUP =====
    @app.on_event("startup")
    async def startup_event():
        api_keys = get_api_keys()
        logger.info("=" * 60)
        logger.info("COOLEMS API Startup v5.0.0")
        logger.info("=" * 60)
        scheme = "https" if use_https else "http"
        logger.info(f"Local URL (click to open): {scheme}://127.0.0.1:{server_port}/")
        logger.info(f"Bind Address: {os.environ.get('COOLEMS_CLIENT_HOST', '127.0.0.1')}:{server_port}")
        logger.info(f"CORS: allowed origins={_cors_origins} (extend via COOLEMS_CORS_ORIGINS, comma-separated); credentials=off")
        logger.info(f"Provider: {provider.name} at {provider.api_url}")
        logger.info(f"Target Model: {model_name}")
        logger.info(f"Timeout: {api_timeout}s")
        logger.info("STREAMING MODE ENABLED - Responses will stream in real-time!")
        # (2026-09-29) the disk file no longer stores keys - this reports available sources
        # (env var + in-memory/legacy), not "keys loaded from .api_client_keys.json".
        logger.info(f"API Authentication: ENABLED ({len(api_keys)} key source(s) available)")
        logger.info("Connection limits: Configurable per ROLE (from profiles.json)")
        logger.info("Tool access: Configurable per ROLE (from profiles.json)")
        logger.info("Model access: Configurable per ROLE (from profiles.json)")
        logger.info("=" * 60)

        agent_identity = agent.get_identity()
        first_line = agent_identity.split("\n")[0] if agent_identity else agent.get_name()
        logger.info(f"DNA loaded: AI name is {agent.get_name()}.")
        logger.info("=" * 60)

        # Start periodic stop_events cleanup task
        chat_bus.start_cleanup()
        logger.info("ChatBus stale-session cleanup task started (runs every 10 minutes)")

        
        logger.info(f"Checking {provider.name} service status...")
        # (2026-08-20) async control channel: one persistent WS instead of a fresh
        # connect+auth per call. We are inside startup_event() -- the loop is running.
        is_healthy, error_msg, models = await provider.health_check_async(force=True)

        if is_healthy:
            logger.info(f"{provider.name} is running. Available models: {models}")
        else:
            logger.warning(f"{provider.name} check failed: {error_msg}")
            logger.info(f"Attempting to start {provider.name} server automatically...")
            started = provider.start_server()
            if started:
                is_healthy, error_msg, models = await provider.health_check_async(force=True)
                if is_healthy:
                    logger.info(f"{provider.name} started successfully. Available models: {models}")
                else:
                    logger.error(f"{provider.name} started but health check still failing: {error_msg}")
            else:
                logger.error(f"Failed to start {provider.name} server automatically.")
                logger.error(f"  Please start {provider.name} service manually.")
        logger.info("=" * 60)

    # ===== SHUTDOWN (2026-08-20) =====
    @app.on_event("shutdown")
    async def shutdown_event():
        """Tear down the persistent control channel cleanly."""
        # (2026-09-10 stale-task fix, user contract): when the CLIENT backend goes away,
        # EVERY live chat session must be stopped - no zombie turns keep streaming after
        # the window is gone. stop_all() is bounded and cross-loop safe; it runs BEFORE
        # anything else so sessions get a clean 'done' frame while sockets are still alive.
        try:
            await chat_bus.stop_all(reason="client-shutdown")
        except Exception as e:  # pragma: no cover - shutdown must never raise
            logger.warning(f"stop_all on shutdown failed (non-fatal): {e}")
        # (2026-09-08 multi-chat) stop the bus' cleanup task first
        chat_bus.stop_cleanup()
        try:
            await provider.close_control_channel()
        except Exception as e:
            logger.debug(f"Control channel close on shutdown: {e}")

    # ===== SHUTDOWN-ALL ENDPOINT (2026-09-10 stale-task fix) ======================
    # The UI calls this from beforeunload/pagehide: "anytime we close the UI, all tasks
    # must be stopped". Loopback-only on purpose - stopping every session is a local
    # owner action and must never be reachable by remote LAN clients.
    @app.post("/api/shutdown-all")
    async def shutdown_all_sessions(request):
        from app.auth import _is_localhost_client

        if not _is_localhost_client({
                "client": (request.client.host if request.client else "", 0),
                "headers": [(b"host", (request.headers.get("host") or "").encode())],
            }):
            return JSONResponse(
                status_code=403,
                content={"status": "error", "message": "shutdown-all is only allowed from this machine."}
            )

        try:
            result = await chat_bus.stop_all(reason="ui-closed")
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"stop_all(ui-closed) failed: {e}")
            return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

        logger.info(f"[SHUTDOWN-ALL] UI closed/crashed - stopped all live sessions: {result}")
        return {"status": "ok", **result}

    # ===== Include route modules =====
    app.include_router(create_conversations_router(db_path=db_path, model_name=model_name, agent=agent))
    app.include_router(create_files_router())
    app.include_router(create_downloads_router())  # /api/setup/status (2026-09-21)
    app.include_router(create_agent_router(
        provider=provider,
        model_name=model_name,
        api_timeout=api_timeout,
        agent=agent,
        stop_events=stop_events,
    ))

    # ===== WEBSOCKET =====
    # The WebSocket handler now receives api_keys for authentication
    # and passes the API key through to the tool orchestrator
    ws_handler = create_websocket_handler(
        db_path=db_path,
        provider=provider,
        model_name=model_name,
        api_timeout=api_timeout,
        agent=agent,
        tool_orchestrator=tool_orchestrator,
        stop_events=stop_events,
        bus=chat_bus,
        api_keys=get_api_keys(),  # Pass API keys for authentication
    )
    app.websocket("/ws/chat/{conv_id}")(ws_handler)

    return app
