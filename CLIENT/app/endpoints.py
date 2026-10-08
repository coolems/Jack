"""
COOLEMS API Endpoints - Thin orchestrator that assembles all route modules.
WebSocket is handled separately in websocket.py.

Route modules:
  - conversations.py : /api/conversations/* (CRUD, messages, truncate, reset)
  - files.py         : /api/upload, /api/tree, /api/download, /api/file-content, /api/open
  - agent.py         : /api/models, /api/status, /api/agent/*, /api/stop/*, /api/my-permissions
  - downloads.py     : /api/setup/status?tool=... (live venv/pip/model setup progress)
  - this module      : GET /api/setup/boot (live boot status, public read-only)

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
               agent, tool_orchestrator, stop_events, server_port: int, use_https=False,
               deferred_boot=None):
    """Create and configure the FastAPI application with all endpoints.

        deferred_boot (2026-10-07 setup-mode UI fix): an async callable that performs the
        provider/agent boot in the BACKGROUND on this app's event loop. Setup mode (no API key
        configured yet) passes it so the UI starts IMMEDIATELY instead of blocking for up to
        30 minutes behind a bootstrap that cannot authenticate without a key; every non-public
        /api/* stays locked by APIMiddleware until a key exists, and while the boot task is
        still running keyed requests get 503 "retry shortly" (see app.auth + keys.is_boot_pending).
        Normal boots pass None - behavior is exactly as before.
        """
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

    # ===== LIVE BOOT STATUS (2026-10-07 setup-mode UI fix) =====
    # Public read-only route (APIMiddleware whitelists GET /api/setup/boot in EVERY mode):
    # while setup mode locks every other /api/* path, the Settings modal polls this to
    # show what boot is doing RIGHT NOW ("waiting for your API key" vs "connecting to
    # SERVER..." with attempt count + budget left). No credential material ever leaves.
    @app.get("/api/setup/boot")
    async def setup_boot_status():
        from app.keys import get_api_keys, is_setup_mode
        try:
            from app.providers.bootstrap import bootstrap_status as _bs
            boot = _bs()
        except Exception:  # pragma: no cover - status must never break the UI
            boot = {"phase": "idle", "attempts": 0, "last_error": "", "budget_sec": 0.0, "remaining_sec": 0.0}
        return {
            "setup_mode": is_setup_mode(),          # credential-level: True while no usable key source exists
            "has_key_source": len(get_api_keys()) > 0,  # what the auth middleware actually gates on
            **boot,
        }

    # ===== STARTUP =====
    # (2026-10-07 setup-mode UI fix): deferred-boot mode runs the provider/agent boot as a
    # background task on THIS loop; startup_event only starts it and skips every
    # provider/agent access while those are still None. Normal boots: unchanged behavior.
    _boot_state = {"task": None}

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
        if provider is not None:
            logger.info(f"Provider: {provider.name} at {provider.api_url}")
        else:
            logger.info("Provider: starting in background (setup mode - live status on GET /api/setup/boot)")
        logger.info(f"Target Model: {model_name or '<auto-selected after boot>'}")
        logger.info(f"Timeout: {api_timeout}s")
        logger.info("STREAMING MODE ENABLED - Responses will stream in real-time!")
        # (2026-10-08) this reports the number of available key SOURCES: config file
        # (CLIENT/config/.api_client_keys.json, plaintext single row) + env var + runtime.
        logger.info(f"API Authentication: ENABLED ({len(api_keys)} key source(s) available)")
        logger.info("Connection limits: Configurable per ROLE (from profiles.json)")
        logger.info("Tool access: Configurable per ROLE (from profiles.json)")
        logger.info("Model access: Configurable per ROLE (from profiles.json)")
        logger.info("=" * 60)

        if agent is not None:
            agent_identity = agent.get_identity()
            first_line = agent_identity.split("\n")[0] if agent_identity else agent.get_name()
            logger.info(f"DNA loaded: AI name is {agent.get_name()}.")
        else:
            logger.info("DNA: loading in background (setup mode - delivered by SERVER after key auth)")
        logger.info("=" * 60)

        # Start periodic stop_events cleanup task
        chat_bus.start_cleanup()
        logger.info("ChatBus stale-session cleanup task started (runs every 10 minutes)")

        # ===== DEFERRED BOOT (2026-10-07 setup-mode UI fix) =====
        # The provider/agent boot runs as a background task on THIS event loop. It keeps
        # retrying the SERVER bootstrap until it succeeds or its budget expires; the phase
        # flips waiting_key -> bootstrapping automatically once the user sets a key in the
        # UI (bootstrap._refresh_phase re-reads the key source every attempt). Routers are
        # safe with provider/agent = None: APIMiddleware blocks every non-public /api/*
        # while no key exists, and returns 503 "retry shortly" until this task finishes.
        if deferred_boot is not None:
            async def _run_deferred():
                try:
                    await deferred_boot()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # One actionable line - the UI stays alive so the user can fix the key /
                    # SERVER address and restart. bootstrap_framework already logged details.
                    logger.error(f"[FATAL] Background provider boot failed: {e}")

            _boot_state["task"] = asyncio.create_task(_run_deferred(), name="deferred-provider-boot")
            logger.info("Deferred provider/agent boot started as background task (setup mode)")

        if provider is not None:
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
        # (2026-10-07 setup-mode UI fix): stop a still-running deferred boot task first so
        # it cannot outlive the loop ("Task was destroyed" noise / late framework install).
        _bt = _boot_state.get("task") if isinstance(_boot_state, dict) else None
        if _bt is not None and not _bt.done():
            _bt.cancel()
            try:
                await asyncio.wait({_bt}, timeout=5.0)
            except Exception as e:  # pragma: no cover - shutdown must never raise
                logger.debug(f"Deferred boot task cancel wait failed (non-fatal): {e}")
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
        if provider is not None:
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
