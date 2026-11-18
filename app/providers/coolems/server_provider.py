"""CoolemsServerProvider - the gateway WebSocket server.

Accepts encrypted WebSocket connections, authenticates by API key + profile, filters
models/tools by user role, and relays to the local LLM backend chosen by
config.SERVER_BACKEND (llama or strata). With USE_WEB_SERVER set it connects to the web
relay as a "brain" instead of listening locally. Pure asyncio: every server task is an
asyncio.Task on the main event loop.

Siblings: ssl_context.py, tool_scanner/, direct_ws/, ws_brain_relay/, chat_relay.py.
"""

import asyncio
import logging
from typing import List, Dict, Any, Tuple, Optional

from config import (
    PROVIDER_DEFAULT_TIMEOUT,
    COOLEMS_SERVER_HOST,
    USE_WEB_SERVER,
    HEALTH_CHECK_INTERVAL_SEC,
)
from ..base import BaseProvider
from ..token_stats import TokenStats

# Split module imports
from .ssl_context import _create_direct_ws_ssl_context
from .ws_client_handler import handle_direct_ws_client
from .ws_brain_relay import connect_to_web_relay
from .connection_enforcement import rate_limiter
from .chat_relay import relay_chat_websocket_direct, relay_chat_websocket

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")


class CoolemsServerProvider(BaseProvider):

    def __init__(self, api_url: str, timeout: int = PROVIDER_DEFAULT_TIMEOUT):
        self._api_url = api_url
        self._timeout = timeout
        self._local_provider = None
        self._running = False  # Simple boolean flag for shutdown signaling (replaces threading.Event)

        # WebSocket relay state
        self._ws_connected = False

        # Smart auth gateway for profile-based permission enforcement
        from ...auth_gateway import AuthGateway
        self._auth_gateway = AuthGateway()

        # asyncio task handles (replaces threads — stored properly so they can be cancelled)
        self._direct_ws_task: Optional[asyncio.Task] = None
        self._relay_brain_task: Optional[asyncio.Task] = None
        # L5 fix: explicit idle sentinel for the inactive mode so start_all_servers()
        # never passes an anonymous asyncio.sleep(float('inf')) coroutine into gather
        # (those were only ever cleaned up by GC on cancel). Created in
        # start_all_servers(), cancelled explicitly in stop_all_servers().
        self._idle_sentinel_task: Optional[asyncio.Task] = None

        # (2026-09-08 multi-chat) chat request queue + worker pool over the llama backend registry.
        # Created lazily on the boot event loop by _ensure_chat_queue(); unit tests may build their own.
        self._chat_queue = None
        self._worker_pool = None

    @property
    def name(self): return "coolems_server"

    @property
    def api_url(self): return self._api_url

    # --- Lazy init local backend provider (llama.cpp or Strata) ---

    def _get_local_provider(self):
        """The local inference backend this relay talks to, chosen by config.SERVER_BACKEND.

        "strata" -> StrataProvider (same wire format, Bearer key, attach-only),
        "deepseek" -> DeepSeekProvider (hosted API, own payload/response transform,
        attach-only), "llama" -> LlamaProvider. Every relay path below is identical either way.
        """
        if self._local_provider is None:
            import config
            backend = getattr(config, "SERVER_BACKEND", "llama")
            if backend == "strata":
                from ..strata.provider import StrataProvider
                self._local_provider = StrataProvider(self._api_url, self._timeout)
                logger.info(f"[SERVER] Local StrataProvider initialized at {self._api_url} "
                            f"(attach-only; auth={'Bearer key' if self._local_provider.headers else 'none'})")
            elif backend == "deepseek":
                from ..deepseek.provider import DeepSeekProvider
                self._local_provider = DeepSeekProvider(self._api_url, self._timeout)
                logger.info(f"[SERVER] Local DeepSeekProvider initialized at {self._api_url} "
                            f"(hosted API, attach-only; auth={'Bearer key' if self._local_provider.headers else 'none'})")
            else:
                from ..llama.provider import LlamaProvider
                self._local_provider = LlamaProvider(self._api_url, self._timeout)
                logger.info(f"[SERVER] Local LlamaProvider initialized at {self._api_url}")
        return self._local_provider

    # --- Chat relay methods (delegate to split modules) ---

    async def _relay_chat_websocket_direct(self, msg: dict, ws):
        """Delegate to chat_relay module."""
        await relay_chat_websocket_direct(self, msg, ws)

    async def _relay_chat_websocket(self, msg: dict, ws, user_info=None):
        """Delegate to chat_relay module.

             *user_info* is the RELAYED CLIENT's resolved profile; it drives
            per-client tool-schema filtering and response routing inside relay_chat_websocket().
        """
        await relay_chat_websocket(self, msg, ws, user_info)

    # --- Direct WebSocket Server (SECURE MODE - TLS encrypted + Auth + Profile Enforcement) ---

    async def _run_direct_ws_server(self):
        """Start the direct WebSocket server with TLS encryption. Runs as asyncio.Task on main loop."""
        import websockets

        from config import (
            COOLEMS_DIRECT_WS_PORT,
            COOLEMS_DIRECT_WS_CERT_PATH,
            COOLEMS_DIRECT_WS_KEY_PATH, WEB_RELAY_MAX_MESSAGE_SIZE,
            WEB_RELAY_HEARTBEAT_INTERVAL, WEB_RELAY_HEARTBEAT_TIMEOUT,
        )

        host = COOLEMS_SERVER_HOST
        port = COOLEMS_DIRECT_WS_PORT

        logger.info(f"[SERVER] Starting Direct WebSocket server on {host}:{port} (TLS=always ON)")

        ssl_context = None
        # TLS always enabled
        try:
            ssl_context = _create_direct_ws_ssl_context(COOLEMS_DIRECT_WS_CERT_PATH, COOLEMS_DIRECT_WS_KEY_PATH)
            logger.info(f"[SERVER] Direct WS server TLS context created successfully")
        except Exception as e:
            logger.critical(f"[SERVER] TLS initialization FAILED - refusing to start without encryption: {e}")
            raise RuntimeError("Cannot start coolems_server: TLS/SSL certificate generation failed. "
                             "Ensure 'cryptography' package is installed and config directory is writable.") from e

        try:
            async with websockets.serve(
                self._handle_direct_ws_client,
                host, port,
                ssl=ssl_context,
                max_size=WEB_RELAY_MAX_MESSAGE_SIZE,
                ping_interval=WEB_RELAY_HEARTBEAT_INTERVAL,
                ping_timeout=WEB_RELAY_HEARTBEAT_TIMEOUT,
            ) as server:  # noqa: F841 - binding keeps the serving context alive for the loop below
                scheme = "wss" if ssl_context else "ws"
                logger.info(f"[SERVER] Direct WS server NOW LISTENING on {scheme}://{host}:{port}")

                # Run until shutdown flag is set
                while self._running:
                    await asyncio.sleep(1)

        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.error(f"[SERVER] Direct WS server error: {e}", exc_info=True)

    async def _handle_direct_ws_client(self, ws):
        """Delegate to ws_client_handler module."""
        await handle_direct_ws_client(ws, self)

    # --- WebSocket Relay Brain Mode ---

    async def _connect_to_web_relay(self):
        """Delegate to ws_brain_relay module."""
        await connect_to_web_relay(self)

    # --- Server Lifecycle Management (asyncio Tasks — no threads) ---

    async def _ensure_llm_healthy(self):
        """Start the local LLM backend and wait until it reports healthy. Returns True/False.

        Called from the boot sequence (code.py wraps init in asyncio.run). WebSocket tasks are
        started later by provider.start_all_servers().
        """
        local = self._get_local_provider()
        ok = await local.start_server()

        for i in range(10):
            healthy, _, models = await local.health_check(force=True)
            if healthy:
                logger.info(f"[SERVER] LLM healthy with {len(models)} model(s)")
                break
            await asyncio.sleep(2 ** min(i, 3))
        else:
            logger.warning("[SERVER] LLM not healthy within timeout")

        return ok

    async def start_server(self) -> bool:
        """Async entry point — called from the boot sequence (code.py asyncio.run(_boot())).

        Starts the local llama.cpp server and verifies health. Returns quickly so
        initialization can continue. WebSocket tasks are started later by code.py
        via await provider.start_all_servers()."""
        return await self._ensure_llm_healthy()

    # --- (2026-09-08 multi-chat) chat queue + worker pool lifecycle -----------------

    async def _ensure_chat_queue(self) -> None:
        """Create the request queue and one worker per llama backend (idempotent).

        Runs on the SERVING event loop: queue Condition, HTTP pool, health poller and workers all
        bind to it, so health/capacity are re-probed here after boot.
        """
        if self._chat_queue is not None:
            return

        from app.server_queue import RequestQueue, WorkerPool, set_queue, set_worker_pool
        from ..llama.backend_registry import get_registry

        reg = get_registry()
        if reg is None:
            # No registry means no backend could have been started. Do NOT publish a
            # queue: requests would enqueue and never run. Clients get the clear
            # "request queue unavailable" error instead of hanging.
            logger.error(
                "[SERVER] LLM backend registry missing at queue init - refusing to create "
                "a chat queue (every request would hang). Check the LLM startup logs."
            )
            set_queue(None)
            set_worker_pool(None)
            return

        queue = RequestQueue()
        set_queue(queue)
        self._chat_queue = queue

        # (2026-10 fleet) Boot ran on a temporary asyncio.run() loop, so the registry's
        # health poller died with it and capacities were frozen at boot values. Refresh
        # /health + /props HERE on the serving loop and move the poller onto this loop
        # BEFORE sizing the worker pool (workers are one per discovered slot).
        await reg.probe_all()
        reg.log_fleet_table(reg.registry_title() + " (serving loop)")
        reg.start_health_poller(interval_sec=HEALTH_CHECK_INTERVAL_SEC)

        pool = WorkerPool(reg, queue)
        pool.start()
        set_worker_pool(pool)
        self._worker_pool = pool

        queue.start_stats_logger()
        logger.info("[SERVER] Chat request queue + worker pool ready (multi-chat sessions)")

    async def _stop_chat_queue(self) -> None:
        """Stop workers, drain the backlog and clear singletons (shutdown path)."""
        aborted = 0
        if self._worker_pool is not None:
            await self._worker_pool.stop()
            self._worker_pool = None
        if self._chat_queue is not None:
            # abort anything still queued so no worker wakes up mid-shutdown
            aborted = await self._chat_queue.drain_and_cancel_all()
            self._chat_queue.stop_stats_logger()
            self._chat_queue = None
        from app.server_queue import set_queue, set_worker_pool

        set_queue(None)
        set_worker_pool(None)
        if aborted:
            logger.info("[SERVER] Aborted %d queued chat request(s) during shutdown", aborted)

    async def start_all_servers(self) -> bool:
        """Async entry point — called from code.py's _run_coolems_server_async (async context).

        Creates WebSocket tasks on the current event loop and awaits them forever
        until cancelled by stop_all_servers(). Assumes LLM is already started/healthy
        """
        # 1. Set running flag and create WebSocket tasks
        self._running = True

        # (2026-09-08 multi-chat) queue + workers must exist before the first frame can arrive
        await self._ensure_chat_queue()

        if USE_WEB_SERVER:
            # Connect to external web relay as brain server
            logger.info(f"[SERVER] Using EXTERNAL web relay mode: {USE_WEB_SERVER}")
            self._relay_brain_task = asyncio.create_task(
                self._connect_to_web_relay(), name="coolems-ws-brain"
            )
            logger.info("[SERVER] Web relay brain task started")
        else:
            # Start Direct WebSocket server with TLS (secure PC-to-PC) — ALWAYS ON when not using web relay
            # (2026-11 review fix A5) Read the attribute off the config MODULE, not a value
            # snapshot: code.py's _apply_port_override() mutates config.COOLEMS_DIRECT_WS_PORT
            # after this module was imported, so `from config import COOLEMS_DIRECT_WS_PORT`
            # would bind the pre-override number and the banner here could name a port that
            # _run_direct_ws_server() (line ~137, attribute read) does not bind. Attribute
            # access keeps banner and bind in agreement regardless of import order.
            import config as _cfg
            _direct_ws_port = _cfg.COOLEMS_DIRECT_WS_PORT
            logger.info(f"[SERVER] Using DIRECT WEBSOCKET mode on port {_direct_ws_port} (TLS encrypted)")
            self._direct_ws_task = asyncio.create_task(
                self._run_direct_ws_server(), name="coolems-direct-ws"
            )
            logger.info("[SERVER] Direct WebSocket server task started")

        # 2. Await both tasks forever (until cancelled by stop_all_servers)
        # L5 fix: the inactive mode gets an explicit, named sentinel task instead of
        # anonymous asyncio.sleep(float('inf')) coroutines — it is tracked on self and
        # cancelled explicitly in stop_all_servers() rather than only GC'd on cancel.
        if not self._direct_ws_task and not self._relay_brain_task:
            raise RuntimeError("start_all_servers(): no WebSocket mode selected")

        if self._idle_sentinel_task and not self._idle_sentinel_task.done():
            self._idle_sentinel_task.cancel()  # defensive: re-entry safety
        self._idle_sentinel_task = asyncio.create_task(
            asyncio.sleep(float('inf')), name="coolems-idle-sentinel"
        )

        # 2b. Await until an ACTIVE server task terminates (FIRST_COMPLETED).
        #     (2026-10-11 fix) The idle sentinel must NOT be awaited: if the real
        #     task dies (TLS init failure, port in use), gather() on
        #     (real_task, infinite_sentinel) would hang forever with no listener.
        #     asyncio.wait() over only the active tasks fails fast instead.
        active_tasks = [t for t in (self._direct_ws_task, self._relay_brain_task) if t is not None]

        try:
            done, _pending = await asyncio.wait(active_tasks, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            logger.info("[SERVER] Server tasks cancelled (shutdown)")
            return True  # code.py finally -> stop_all_servers()

        # A real server task ended. Distinguish shutdown-cancel vs genuine death.
        died = False
        for t in done:
            if t.cancelled():
                continue  # normal shutdown path (stop_all_servers cancelled it)
            exc = t.exception()
            if exc is not None:
                logger.critical(f"[SERVER] {t.get_name()} died with unhandled exception - exiting", exc_info=exc)
                raise exc  # fail fast: non-zero exit; code.py finally still runs stop_all_servers()
            logger.critical(f"[SERVER] {t.get_name()} exited without error - server NOT listening; exiting")
            died = True

        if not died:
            return True  # every done task was cancelled -> clean shutdown

        raise RuntimeError("start_all_servers(): active WebSocket task terminated unexpectedly")

    async def stop_all_servers(self) -> None:
        """Gracefully shut down all server tasks and the local LLM.

        Cancels both WebSocket tasks with a timeout, unloads the model, and closes
        the shared HTTP client pool to release pooled connections.
        Must be called from within an async context (on the same event loop as tasks).
        """
        logger.info("[SERVER] Stopping all servers...")
        self._running = False

        # Stop rate limiter cleanup task first (proper lifecycle management)
        await rate_limiter.stop()

        # (2026-09-08 multi-chat) stop the worker pool + drain the queue first
        await self._stop_chat_queue()

        # Cancel WebSocket tasks with timeout
        tasks_to_cancel = []
        if self._direct_ws_task and not self._direct_ws_task.done():
            tasks_to_cancel.append(self._direct_ws_task)
        if self._relay_brain_task and not self._relay_brain_task.done():
            tasks_to_cancel.append(self._relay_brain_task)
        # L5 fix: cancel the idle sentinel explicitly (it is a real Task now,
        # not an anonymous coroutine abandoned to GC).
        if self._idle_sentinel_task and not self._idle_sentinel_task.done():
            tasks_to_cancel.append(self._idle_sentinel_task)

        for task in tasks_to_cancel:
            logger.info(f"[SERVER] Cancelling task: {task.get_name()}")
            task.cancel()

        # Wait for tasks to finish with timeout (on current event loop)
        if tasks_to_cancel:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("[SERVER] Task cleanup timed out - forcing exit")
            except Exception as e:
                logger.warning(f"[SERVER] Task cleanup issue: {e}")

        # Unload local LLM model and close HTTP client pool
        if self._local_provider:
            self._local_provider.unload_model(None)
            await self._local_provider.close()  # Close shared httpx/requests pools

        logger.info("[SERVER] All servers stopped.")

    def stop_server(self) -> None:
        """Synchronous no-op required by the BaseProvider interface.

        Shutdown is the async path: stop_all_servers(), called from
        code.py's _run_coolems_server_async.
        """
        logger.info("[SERVER] stop_server() called — no-op, shutdown handled by async path")

    # --- BaseProvider Interface (all delegate to local LlamaProvider) ---

    async def chat_stream(
        self, model: str, messages: List[Dict], websocket: Any,
        temperature: float = 0.7, enable_thinking=None,
        conv_id: str = None, tools: list = None,
        stop_event: Optional[asyncio.Event] = None,
        context_window: Optional[int] = None,
    ) -> Tuple[str, TokenStats]:
        """chat_stream for the local UI (same PC) - delegates to the local backend provider.

        enable_thinking accepts a bool or a canonical thinking level.
        """
        return await self._get_local_provider().chat_stream(
            model=model, messages=messages, websocket=websocket,
            temperature=temperature, enable_thinking=enable_thinking,
            conv_id=conv_id, tools=tools, stop_event=stop_event,
            context_window=context_window,
        )

    async def chat_non_streaming(self, model: str, messages: List[Dict],
                                  temperature: float = 0.7,
                                  enable_thinking=None) -> str:
        return await self._get_local_provider().chat_non_streaming(
            model=model, messages=messages, temperature=temperature,
            enable_thinking=enable_thinking,
        )

    def build_payload(self, model: str, messages: List[Dict], *, stream: bool = True,
                      temperature: float = 0.7, enable_thinking=None,
                      tools: list = None) -> Dict:
        """Build a backend payload. enable_thinking is a bool or a canonical thinking level;
        the serving endpoint's ThinkingPolicy translates a level into chat_template_kwargs.
        """
        return self._get_local_provider().build_payload(
            model=model, messages=messages, stream=stream,
            temperature=temperature, tools=tools, enable_thinking=enable_thinking,
        )

    async def health_check(self, force: bool = False) -> Tuple[bool, str, List[str]]:
        return await self._get_local_provider().health_check(force=force)

    async def get_current_model(self, force: bool = False):
        """Currently loaded model from the local backend, via _get_local_provider() so a call
        before the first chat still answers.
        """
        return await self._get_local_provider().get_current_model(force=force)

    async def get_models_list(self) -> List[Dict]:
        return await self._get_local_provider().get_models_list()

    def unload_model(self, model: str):
        if self._local_provider: self._local_provider.unload_model(model)

    def reload_model(self, model: str):
        if self._local_provider: self._local_provider.reload_model(model)

    def status_message(self, error: Exception, model_name: str = None,
                       timeout: int = None) -> str:
        return self._get_local_provider().status_message(
            error, model_name=model_name, timeout=timeout
        )

    async def ocr_transcribe(self, image_base64: str, ocr_models: List[str]) -> str:
        return await self._local_provider.ocr_transcribe(
            image_base64=image_base64, ocr_models=ocr_models
        ) if self._local_provider else ""