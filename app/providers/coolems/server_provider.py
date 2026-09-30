"""CoolemsServerProvider -- Smart Gateway WebSocket server.

Accepts encrypted WebSocket connections from coolems_client, authenticates via API key + profile,
filters models/tools by user role, then relays to local LlamaProvider. Enforces permissions.

When USE_WEB_SERVER is configured in config.py:
- Connects TO the web relay as a "brain" server instead of listening locally

The logic here:
1. Start/stop local llama-server process (delegated to LlamaProvider)
2. Listen on Direct WebSocket port for client connections OR connect to web relay as brain
3. Authenticate clients via API key + profile lookup (AuthGateway)
4. Filter models/tools by user role before relaying to LLM
5. Relay authenticated/filtered requests to LlamaProvider and responses back

Pure asyncio — no threads. All server tasks run on the main event loop as asyncio.Tasks.

MODULE SPLIT:
  - ssl_context.py        → Self-signed cert generation
  - tool_scanner/         → package: AST tool scanning + source code serving (split 2026-09-07)
  - direct_ws/            → Direct WS client message routing (split 2026-09-07; ws_client_handler.py is now a shim)
  - ws_brain_relay.py     → Web relay brain connection logic
  - chat_relay.py         → Chat forwarding to LLM
"""

import asyncio
import logging
from typing import List, Dict, Any, Tuple, Optional

from config import (
    PROVIDER_DEFAULT_TIMEOUT,
    COOLEMS_SERVER_HOST,
    USE_WEB_SERVER,
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

    # --- Lazy init local LlamaProvider ---

    def _get_local_provider(self):
        if self._local_provider is None:
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

            (2026-09-01 S1 fix) *user_info* is the RELAYED CLIENT's resolved profile; it drives
            per-client tool-schema filtering and response routing inside relay_chat_websocket().
            """
        await relay_chat_websocket(self, msg, ws, user_info)

    # --- Direct WebSocket Server (SECURE MODE - TLS encrypted + Auth + Profile Enforcement) ---

    async def _run_direct_ws_server(self):
        """Start the direct WebSocket server with TLS encryption. Runs as asyncio.Task on main loop."""
        import websockets

        from config import (
            COOLEMS_SERVER_HOST, COOLEMS_DIRECT_WS_PORT,
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
            ) as server:
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
        """Async: start local llama.cpp server and wait for health check.

        Called by the boot sequence (code.py wraps init in asyncio.run) - returns True/False.
        Does NOT start WebSocket tasks — those are started later by code.py via
        await provider.start_all_servers().

        (2026-09 threadless refactor): async - the LLM launch and health polling run on the
        same event loop with await asyncio.sleep() backoff instead of blocking time.sleep."""
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

        Must run on the boot event loop: asyncio.Queue binds to the running loop. The
        registry was already built by LlamaProvider.start_server() during _ensure_llm_healthy().
        """
        if self._chat_queue is not None:
            return

        from app.server_queue import RequestQueue, WorkerPool, set_queue, set_worker_pool
        from ..llama.backend_registry import get_registry

        queue = RequestQueue()
        set_queue(queue)
        self._chat_queue = queue

        reg = get_registry()
        if reg is not None:
            pool = WorkerPool(reg, queue)
            pool.start()
            set_worker_pool(pool)
            self._worker_pool = pool
        else:  # pragma: no cover - boot always builds the registry first
            logger.warning("[SERVER] LLM backend registry missing at queue init - chat requests will fail")

        queue.start_stats_logger()
        logger.info("[SERVER] Chat request queue + worker pool ready (multi-chat sessions)")

    async def _stop_chat_queue(self) -> None:
        """Stop workers, drain the backlog and clear singletons (shutdown path)."""
        if self._worker_pool is not None:
            await self._worker_pool.stop()
            self._worker_pool = None
        if self._chat_queue is not None:
            # abort anything still queued so no worker wakes up mid-shutdown
            aborted = self._chat_queue.drain_and_cancel_all()
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
        (start_server() was called earlier during initialization).
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
            from config import COOLEMS_DIRECT_WS_PORT
            logger.info(f"[SERVER] Using DIRECT WEBSOCKET mode on port {COOLEMS_DIRECT_WS_PORT} (TLS encrypted)")
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

        try:
            await asyncio.gather(
                self._direct_ws_task if self._direct_ws_task else self._idle_sentinel_task,
                self._relay_brain_task if self._relay_brain_task else self._idle_sentinel_task,
                return_exceptions=True
            )
        except asyncio.CancelledError:
            logger.info("[SERVER] Server tasks cancelled (shutdown)")

        return True

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
        """Legacy synchronous wrapper — no-op. Shutdown is handled entirely by the
        async path (stop_all_servers()) called from code.py's _run_coolems_server_async.

        Kept for BaseProvider interface compatibility. Does nothing because there are
        no tasks to cancel in sync context (they run on a different event loop).
        """
        logger.info("[SERVER] stop_server() called — no-op, shutdown handled by async path")

    # --- BaseProvider Interface (all delegate to local LlamaProvider) ---

    async def chat_stream(
        self, model: str, messages: List[Dict], websocket: Any,
        temperature: float = 0.7, enable_thinking: bool = False,
        conv_id: str = None, tools: list = None,
        stop_event: Optional[asyncio.Event] = None
    ) -> Tuple[str, TokenStats]:
        """Local UI calls this directly (same PC) -> delegates to LlamaProvider."""
        return await self._get_local_provider().chat_stream(
            model=model, messages=messages, websocket=websocket,
            temperature=temperature, enable_thinking=enable_thinking,
            conv_id=conv_id, tools=tools, stop_event=stop_event
        )

    async def chat_non_streaming(self, model: str, messages: List[Dict],
                                  temperature: float = 0.7) -> str:
        return await self._get_local_provider().chat_non_streaming(
            model=model, messages=messages, temperature=temperature
        )

    def build_payload(self, model: str, messages: List[Dict], *, stream: bool = True,
                      temperature: float = 0.7, enable_thinking: bool = False,
                      tools: list = None) -> Dict:
        return self._get_local_provider().build_payload(
            model=model, messages=messages, stream=stream,
            temperature=temperature, enable_thinking=enable_thinking, tools=tools
        )

    async def health_check(self, force: bool = False) -> Tuple[bool, str, List[str]]:
        return await self._get_local_provider().health_check(force=force)

    async def get_current_model(self, force: bool = False):
        """Get currently loaded model name from local LlamaProvider.

        2026-08-18 FIX: used the lazy _get_local_provider() like every other method — before,
        a call before first chat returned None because it read self._local_provider directly
        (None until lazily initialized)."""
        # (2026-08-23 fix) pass force through so a post-switch verification can
        # bypass the fresh model-state cache and confirm the new model immediately.
        return await self._get_local_provider().get_current_model(force=force)

    async def switch_model(self, model_filename: str) -> Tuple[bool, str]:
        """Switch to a different model on the local LlamaProvider (async)."""
        if not self._local_provider:
            return False, "Local provider not initialized"
        return await self._get_local_provider().switch_model(model_filename)

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