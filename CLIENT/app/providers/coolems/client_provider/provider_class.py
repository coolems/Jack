"""CoolemsClientProvider -- Secure WebSocket pipe to remote coolems_server.

Opens encrypted WebSocket connection, forwards params unchanged, relays chunks back, closes.
No intelligence. No modification. Just secure transport.

Direct mode (Settings UI Connection Mode = "direct", the default):
  Connects directly to the SERVER via wss:// with TLS encryption on LAN.

Web relay mode (Settings UI Connection Mode = "web_relay"):
  Connects ONLY to the web relay address saved in settings.json - that is all the
  CLIENT needs. The home SERVER reaches the same relay itself; no SERVER address is
  used or required by this CLIENT in relay mode.

MODULE SPLIT (2026-10):
  - client_provider/connection_modes.py -> model-switch flag + live connection-mode/address helpers
  - client_provider/ws_connect.py       -> _AccumulatingWS + the fail-closed relay_ws_connect helper
  - client_provider/provider_class.py   -> the CoolemsClientProvider class (thin delegations)
  - ssl_helper.py / direct_mode.py / relay_mode.py / tool_requests.py / control_channel.py (siblings)

The flat CLIENT/app/providers/coolems/client_provider.py file is a re-export facade so
existing imports ('from .client_provider import CoolemsClientProvider, is_model_switching,',
'relay_ws_connect') keep working unchanged.
"""

import asyncio
from typing import List, Dict, Any, Tuple, Optional

# Module-level config imports
from config import (
    PROVIDER_DEFAULT_TIMEOUT,
    COOLEMS_CLIENT_SERVER_ADDRESS,
    get_web_relay_address,
    WEB_RELAY_MAX_MESSAGE_SIZE,
    WEB_RELAY_HEARTBEAT_INTERVAL,
    WEB_RELAY_HEARTBEAT_TIMEOUT,
    WS_HANDSHAKE_TIMEOUT,
    WEB_RELAY_VERIFY_SSL_CERTS,
    # (2026-09-01 S3) optional certificate pin for the relay link
    WEB_RELAY_CERT_PIN_FINGERPRINT,
    COOLEMS_DIRECT_VERIFY_SSL_CERTS,
    COOLEMS_SERVER_CERT_PATH,
    COOLEMS_DIRECT_WS_PORT,
    WEB_RELAY_DEFAULT_PORT,
    MODEL_SWITCH_CLIENT_BUDGET_SEC,
)

# (2026-08-20 dedup) BaseProvider/TokenStats are NO LONGER local disk copies -- they are
# delivered from SERVER with tools_response and installed in sys.modules by the loader.
# The relative imports below resolve through app.providers.__getattr__ (PEP 562 lazy shell),
# so this module can be imported at boot BEFORE delivery; real use happens post-init only.
from ... import BaseProvider
from ... import TokenStats

# Shared helpers from coolems protocol module
from .. import _parse_web_relay_address, _build_websocket_url

# Single source of truth for API key loading (shared across entire CLIENT)
from app.keys import load_coolems_api_key

from .connection_modes import (_live_relay_host_port, _parse_direct_ws_address, _relay_mode)
from .ws_connect import _AccumulatingWS

import logging

logger = logging.getLogger("COOLEMS.Provider.CoolemsClient")

class CoolemsClientProvider(BaseProvider):

    """WebSocket client provider - the ONLY transport to a remote COOLEMS SERVER.



    Two connection modes (selected LIVE from settings.json, Settings UI "Connection Mode";

    default is direct):

      * Direct mode  ("direct")     -> wss://<server>:8080 on the LAN

      * Relay mode   ("web_relay")  -> wss to the RELAY address only (host+port from settings);

                                       the home SERVER is never addressed by this CLIENT



    Every method is a thin delegation to direct_mode.py / relay_mode.py /

    control_channel.py; this class holds no protocol logic of its own.

    Outbound auth key comes from app.keys.load_coolems_api_key(): the in-memory runtime key (UI
    session), then <CLIENT>/config/.api_client_keys.json (plaintext single row {email,
    date_acquired, key} - 2026-10-08), then the COOLEMS_CLIENT_API_KEY env var fallback.

    """



    def __init__(self, api_url: str = "", timeout: int = PROVIDER_DEFAULT_TIMEOUT):

        self._timeout = timeout  # overall provider call budget (config.PROVIDER_DEFAULT_TIMEOUT)



        # Config shortcuts for split modules (single source of truth: CLIENT/config)

        self.max_msg_size = WEB_RELAY_MAX_MESSAGE_SIZE

        self.heartbeat_interval = WEB_RELAY_HEARTBEAT_INTERVAL

        self.ping_timeout = WEB_RELAY_HEARTBEAT_TIMEOUT

        self.handshake_timeout = WS_HANDSHAKE_TIMEOUT



        if _relay_mode():

            # (2026-09-23) live relay address from settings.json (UI: Connection Mode).
            # Cached at construction for display; every real connection re-reads it below.
            # NOTE: in web_relay mode the ONLY address that matters is the RELAY's own -
            # the home SERVER address is never used or required by this CLIENT.
            _live_addr = get_web_relay_address()
            if _live_addr:
                self._ws_host, self._ws_port = _parse_web_relay_address(_live_addr, WEB_RELAY_DEFAULT_PORT)
            else:
                # Relay mode selected but no relay host saved yet - keep a safe placeholder;
                # every real connection re-reads settings and reports the actionable error.
                self._ws_host, self._ws_port = "relay-not-configured", WEB_RELAY_DEFAULT_PORT

            self._use_ssl = True

            if _live_addr:
                logger.info(f"[CLIENT] WebSocket relay mode (UI Connection Mode): {self._ws_host}:{self._ws_port}")
            else:
                logger.warning(
                    "[CLIENT] Web relay mode selected but no relay host saved - open Settings and "
                    "enter the web_server_relay address (host + optional port). Connections will be refused until then."
                )

        else:

            addr = COOLEMS_CLIENT_SERVER_ADDRESS

            self._direct_ws_host, self._direct_ws_port = _parse_direct_ws_address(addr, COOLEMS_DIRECT_WS_PORT)

            self._use_direct_ssl = True

            logger.info(f"[CLIENT] Direct WebSocket mode: address={addr}")



    @property

    def name(self) -> str:

        """Provider identifier used in logs and status endpoints."""

        return "coolems_client"



    @property

    def api_url(self) -> str:

        """Display URL of the remote SERVER (relay or direct), for logs/UI only."""

        if _relay_mode():

            _live = get_web_relay_address() or (self._ws_host + (
                ":" + str(self._ws_port) if self._ws_port else ""))
            if not _live or _live == "relay-not-configured":
                return "wss://<relay host not saved - open Settings>"
            return f"wss://{_live}"

        return f"wss://{COOLEMS_CLIENT_SERVER_ADDRESS}"



    # --- API key loader (for split modules) ---



    def _load_api_key(self):

        """Load the coolems API key. Used by split modules."""

        return load_coolems_api_key()



    # --- SSL context builders ---



    @property
    def _is_relay(self) -> bool:
        """(2026-09-23) True while the CLIENT is configured for web-relay mode (live)."""

        return _relay_mode()

    def _build_ssl_context(self):
        """SSL context for RELAY mode - FAIL-CLOSED (2026-09-23 v4).

        The relay runs on a PUBLIC host, so the client->relay link is internet-facing:
        an untrusted transport would expose the API key in the first auth frame. Either
        full CA verification (WEB_RELAY_VERIFY_SSL_CERTS=True - Let's Encrypt etc.) or a
        usable cert pin (WEB_RELAY_CERT_PIN_FINGERPRINT) is required; there is NO silent
        CERT_NONE fallback anymore.

        Returns the ssl context, or None when TLS cannot be trusted - every caller must
        then refuse to connect with an actionable message (see _relay_connect()).
        """
        if not self._use_ssl:

            return None

        from ..ssl_helper import _build_relay_ssl_context

        ctx = _build_relay_ssl_context(WEB_RELAY_VERIFY_SSL_CERTS, WEB_RELAY_CERT_PIN_FINGERPRINT)

        if ctx is not None:

            logger.info("[TLS] Relay mode: trusted TLS (CA verification or cert pin)")

        else:

            logger.error(
                "[TLS] Relay mode REFUSED - no trusted TLS configured. Set WEB_RELAY_VERIFY_SSL_CERTS=True "
                "(public CA, e.g. Let's Encrypt) or WEB_RELAY_CERT_PIN_FINGERPRINT in CLIENT config."
            )

        return ctx

    def _build_direct_ssl_context(self):

        """SSL context for DIRECT mode (honors COOLEMS_DIRECT_VERIFY_SSL_CERTS)."""

        if not self._use_direct_ssl:

            return None

        from ..ssl_helper import _build_verifying_ssl_context, _build_no_verify_ssl_context

        if COOLEMS_DIRECT_VERIFY_SSL_CERTS:

            ctx = _build_verifying_ssl_context(COOLEMS_SERVER_CERT_PATH)

            logger.info("[TLS] Direct mode: certificate verification ENABLED")

            return ctx

        logger.debug("[TLS] Direct mode: certificate verification DISABLED")

        return _build_no_verify_ssl_context()



    def _get_ws_url_and_ssl(self):

        """Get WebSocket URL and SSL context based on current mode."""

        if _relay_mode():

            host, port = _live_relay_host_port() or (self._ws_host, self._ws_port)
            ws_url = _build_websocket_url(host, port, self._use_ssl, "/ws/client")

            ssl_ctx = self._build_ssl_context()

        else:

            ws_url = _build_websocket_url(self._direct_ws_host, self._direct_ws_port, self._use_direct_ssl)

            ssl_ctx = self._build_direct_ssl_context()

        return ws_url, ssl_ctx

    def _get_ws_candidates(self) -> list:
        """Ordered (ws_url, ssl_context) candidates for the next connection attempt.

        Direct mode: one candidate per address from config.get_server_address_list()
        (settings.json 'server_addresses', local first - see that function's docstring).
        Relay mode: a single candidate (the relay itself). The list is read FRESH on
        every call so settings.json edits apply to the very next connection.

        STICKY ORDERING (2026-09-01): when config.get_good_server() has a recorded
        known-good address that is still in the list, it is moved to index 0 - after a
        restart the client goes straight back to the server that worked last time and
        only scans the rest of the list if THAT one is dead (open_with_failover keeps
        the runtime stickiness; this ordering covers cold starts).
        """
        if _relay_mode():

            host, port = _live_relay_host_port() or (self._ws_host, self._ws_port)
            ws_url = _build_websocket_url(host, port, self._use_ssl, "/ws/client")
            ssl_ctx = self._build_ssl_context()
            # (2026-09-23 v4) fail-closed: an untrusted relay transport must never be used -
            # raise with the actionable message instead of connecting without TLS.
            if ssl_ctx is None:
                raise ConnectionError(
                    "Web relay connection refused - no trusted TLS configured. Set WEB_RELAY_VERIFY_SSL_CERTS=True "
                    "(public CA, e.g. a public certificate authority) or WEB_RELAY_CERT_PIN_FINGERPRINT in CLIENT config."
                )
            return [(ws_url, ssl_ctx)]

        from config import get_server_address_list, get_good_server
        addrs = list(get_server_address_list())
        good = get_good_server()
        if good and good in addrs:
            # Stable reorder: known-good first, settings order preserved for the rest.
            addrs.remove(good)
            addrs.insert(0, good)
        candidates = []
        seen = set()
        for addr in addrs:
            try:
                host, port = _parse_direct_ws_address(addr, COOLEMS_DIRECT_WS_PORT)
            except ValueError as e:
                logger.warning(f"[CLIENT] Skipping invalid server address {addr!r}: {e}")
                continue
            if (host, port) in seen:
                continue
            seen.add((host, port))
            candidates.append(
                (_build_websocket_url(host, port, self._use_direct_ssl),
                 self._build_direct_ssl_context())
            )
        if not candidates:  # settings empty/corrupt -> boot-time cached address keeps us alive
            ws_url = _build_websocket_url(self._direct_ws_host, self._direct_ws_port, self._use_direct_ssl)
            candidates.append((ws_url, self._build_direct_ssl_context()))
        return candidates

    # --- Tool request helpers (delegate to tool_requests module) ---



    async def send_tool_request(self, req_type: str, **kwargs) -> Optional[Dict]:

        """Send a tool-related request to SERVER via short-lived WebSocket."""

        from ..tool_requests import send_tool_request as _send_tool_request

        return await _send_tool_request(self, req_type, **kwargs)



    # --- Chat streaming (delegate to direct_mode / relay_mode) ---



    async def chat_stream(

        self, model: str, messages: List[Dict], websocket: Any,

        temperature: float = 0.7, enable_thinking: bool = False,

        conv_id: str = None, tools: list = None,

        stop_event: Optional[asyncio.Event] = None

    ) -> Tuple[str, TokenStats]:

        """Open encrypted WebSocket connection, send request, relay streaming to UI, return final response."""

        if _relay_mode():

            from ..relay_mode import chat_stream_websocket

            return await chat_stream_websocket(

                self, model, messages, websocket, temperature, enable_thinking, conv_id, tools, stop_event

            )

        from ..direct_mode import chat_stream_direct_websocket

        return await chat_stream_direct_websocket(

            self, model, messages, websocket, temperature, enable_thinking, conv_id, tools, stop_event

        )



    # --- Health check (delegate) ---



    async def _async_health_check(self, force: bool = False):

        """Async health probe; dispatches to relay or direct implementation."""

        if _relay_mode():

            from ..relay_mode import health_check_websocket

            return await health_check_websocket(self, force)

        # (2026-08-21 fix) direct branch previously never imported its function -> NameError on first use.

        from ..direct_mode import health_check_direct_websocket

        return await health_check_direct_websocket(self, force)



    async def health_check(self, force: bool = False) -> Tuple[bool, str, List[str]]:
            """Health probe (async).

            (2026-09 threadless refactor): the old one-shot ThreadPoolExecutor + asyncio.run
            bridge is gone. This simply awaits the relay/direct implementation on the
            caller's event loop; every production caller is async now (boot runs its own
            top-level asyncio.run before uvicorn starts)."""
            try:
                return await asyncio.wait_for(self._async_health_check(force), timeout=30)
            except TimeoutError:
                # Bounded wait expired - report the honest status instead of a raw traceback.
                logger.warning("[CLIENT] Health check timed out (SERVER not responding within 30s)")
                return False, "Health check timed out (SERVER not responding)", []
    
    # --- Get models (delegate) ---



    async def _async_get_models(self):

        """Async model list fetch; dispatches to relay or direct implementation."""

        if _relay_mode():

            from ..relay_mode import get_models_websocket

            return await get_models_websocket(self)

        # (2026-08-21 fix) direct branch previously never imported its function -> NameError on first use.

        from ..direct_mode import get_models_direct_websocket

        return await get_models_direct_websocket(self)



    async def get_models_list(self) -> List[Dict]:
            """Model list fetch (async).

            (2026-09 threadless refactor): executor + asyncio.run bridge removed; awaits the
            relay/direct implementation on the caller's loop."""
            try:
                return await asyncio.wait_for(self._async_get_models(), timeout=30)
            except TimeoutError:
                # Bounded wait expired - the caller (agent.py /api/models) decides how to log it.
                # During a model switch this is an EXPECTED state, not an error.
                return []
    
    # --- Get current model (delegate) ---



    async def _async_get_current_model(self):

        """Async current-model query; dispatches to relay or direct implementation."""

        if _relay_mode():

            from ..relay_mode import get_current_model_websocket

            return await get_current_model_websocket(self)

        # (2026-08-21 fix) direct branch previously never imported its function -> NameError on first use.

        from ..direct_mode import get_current_model_direct_websocket

        return await get_current_model_direct_websocket(self)



    async def get_current_model(self, force: bool = False) -> Optional[str]:
            """Current-model query (async).

            (2026-09 threadless refactor): executor + asyncio.run bridge removed; awaits the
            relay/direct implementation on the caller's loop."""
            try:
                return await asyncio.wait_for(self._async_get_current_model(force), timeout=15)
            except TimeoutError:
                logger.debug("[CLIENT] get_current_model timed out (SERVER not responding within 15s)")
                return None
    
        # --- Async control-channel variants (2026-08-20, review item #6) ---
        # These run on the uvicorn loop over ONE persistent authenticated WS instead of
        # opening a fresh connection per call. switch_model intentionally stays
        # off-channel: a model switch restarts the SERVER, killing any live connection.



    async def health_check_async(self, force: bool = False):

        """Health check over the persistent control channel (async context only)."""

        from ..control_channel import get_control_channel

        resp = await get_control_channel(self).request("health_check", timeout=15.0)

        if not resp or resp.get("type") != "health_ok":

            err = (resp or {}).get("message", "no response")

            return (False, err, [])

        models = [m.get("name", "") for m in resp.get("models", [])]

        return (True, "", models)



    async def get_models_list_async(self):

        """Model list over the persistent control channel (async context only)."""

        from ..control_channel import get_control_channel

        resp = await get_control_channel(self).request("models_request", timeout=15.0)

        if not resp or resp.get("type") != "models_response":

            return []

        return resp.get("models", [])



    async def get_current_model_async(self, force: bool = False):

        """Current model over the persistent control channel (async context only)."""

        from ..control_channel import get_control_channel

        # (2026-08-23 fix) force=True tells the SERVER to bypass its fresh in-memory

        # model-state cache and verify via /v1/models - immediate post-switch confirmation.

        kwargs = {"force": True} if force else {}

        resp = await get_control_channel(self).request("current_model", timeout=15.0, **kwargs)

        if not resp or resp.get("type") != "current_model_response":

            return None

        model = resp.get("model", "")

        return model or None



    async def close_control_channel(self):

        """Tear down the control channel (app shutdown). Idempotent."""

        from ..control_channel import get_control_channel

        await get_control_channel(self).close()



    # --- Switch model (delegate) ---



    async def _async_switch_model(self, model_filename: str):

        """Async model switch; dispatches to relay or direct implementation."""

        if _relay_mode():

            from ..relay_mode import switch_model_websocket

            return await switch_model_websocket(self, model_filename)

        # (2026-08-21 fix) direct branch previously never imported its function -> NameError on first use.

        from ..direct_mode import switch_model_direct_websocket

        return await switch_model_direct_websocket(self, model_filename)



    async def switch_model(self, model_filename: str) -> Tuple[bool, str]:
            """Switch the SERVER's loaded model (async).

            The global _model_switch_in_progress flag is managed by agent.py router.

            Bounded wait (MODEL_SWITCH_CLIENT_BUDGET_SEC): the SERVER's own reload wait
            (~120s worst case) plus margin fits inside this budget, so a healthy switch
            always returns its real result. If the budget expires we report it as a clean
            failure - there is no fallback path.

            (2026-09 threadless refactor): the old ThreadPoolExecutor + asyncio.run bridge
            (which double-threaded when called from an async context) is gone; this awaits
            the relay/direct implementation directly on the caller's event loop."""
            try:
                return await asyncio.wait_for(
                    self._async_switch_model(model_filename), timeout=MODEL_SWITCH_CLIENT_BUDGET_SEC
                )
            except TimeoutError:
                logger.error(f"[MODEL SWITCH] Timed out after {MODEL_SWITCH_CLIENT_BUDGET_SEC}s waiting for the switch to complete")
                return False, f"Model switch timed out after {MODEL_SWITCH_CLIENT_BUDGET_SEC}s (SERVER did not confirm the new model loaded)"
    
    # --- Non-streaming chat ---



    async def chat_non_streaming(self, model: str, messages: List[Dict],

                                 temperature: float = 0.7) -> str:

        """Non-streaming chat: runs chat_stream into an in-memory accumulator WS."""

        ws = _AccumulatingWS()

        text, _ = await self.chat_stream(model, messages, ws, temperature=temperature)

        return text



    # --- BaseProvider interface stubs ---



    def build_payload(self, model: str, messages: List[Dict], *, stream: bool = True,

                      temperature: float = 0.7, enable_thinking: bool = False,

                      tools: list = None) -> Dict:

        """Build the request payload (kept minimal - SERVER owns final shaping)."""

        return {"model": model, "messages": messages, "stream": stream,

                "temperature": temperature, "enable_thinking": enable_thinking}



    def unload_model(self, model: str) -> None:

        """No-op - the SERVER owns model lifecycle (no local LLM server)."""

        pass



    def reload_model(self, model: str) -> None:

        """No-op - see unload_model; model loading happens on the SERVER."""

        pass



    def status_message(self, error: Exception, model_name: str = None,

                       timeout: int = None) -> str:

        """Human-friendly one-liner for an error (shown to the user in the UI)."""

        # (2026-08-21 fix) target was previously unbound for non-ConnectionError inputs.

        target = (get_web_relay_address() if _relay_mode() else None) or COOLEMS_CLIENT_SERVER_ADDRESS

        if isinstance(error, ConnectionError):

            return f"Cannot connect to coolems_server at {target}"

        return str(error)



    async def ocr_transcribe(self, image_base64: str, ocr_models: List[str]) -> str:
        """Transcribe an image by delegating to the SERVER (2026-08-23 revive).

        The CLIENT ships pixels + model names over a short-lived authenticated WS;
        the SERVER runs its local LlamaProvider OCR path (the exact GLM-OCR call
        format lives in app/providers/llama/ocr.py) and returns the extracted text.
        """
        if not ocr_models:
            return "ERROR: No OCR models configured."
        # OCR can run long on the SERVER (its budget is config.OCR_TIMEOUT=120 s);
        # give the response wait a matching margin so slow transcriptions don't die at 15 s.
        import config as _cfg
        ocr_recv_timeout = float(getattr(_cfg, "OCR_TIMEOUT", 120)) + 10

        resp = await self.send_tool_request(
            "ocr_request",
            image_base64=image_base64,
            ocr_models=list(ocr_models),
            recv_timeout=ocr_recv_timeout,
        )
        if not resp or resp.get("type") != "ocr_response":
            return f"ERROR: No OCR response from SERVER (got: {resp})"
        text = str(resp.get("text", "")).strip()
        if resp.get("success"):
            return text
        return text or "ERROR: SERVER-side OCR failed."

    def start_server(self) -> bool:

        """Always succeeds - there is nothing local to start; we connect out."""

        logger.info("[CLIENT] CoolemsClient - no local server needed (connecting to remote)")

        return True
