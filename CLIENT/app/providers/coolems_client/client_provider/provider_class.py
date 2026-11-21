"""CoolemsClientProvider -- encrypted WebSocket transport to a remote coolems_server.

Sends request params, relays streamed chunks back, closes. No generation logic here.
Connection mode picks the peer: "direct" -> wss://<server>:<port> on the LAN;
"web_relay" -> the relay address from settings.json only (the SERVER reaches that same
relay itself). Siblings: connection_modes.py, ws_connect.py, ssl_helper.py, direct_mode.py,
relay_mode.py, tool_requests.py, control_channel.py. The package __init__ re-exports
this class and the connection-mode helpers.
"""

import asyncio
from typing import List, Dict, Any, Tuple, Optional

# Module-level config imports
from client_config import (
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

    """WebSocket client provider - the only transport to a remote COOLEMS SERVER.

    Connection mode is read live from settings.json: "direct" -> wss://<server>:8080,
    "web_relay" -> the relay address only. Methods delegate to direct_mode.py / relay_mode.py
    / control_channel.py, so the class holds no protocol logic. The outbound auth key comes
    from app.keys.load_coolems_api_key().
    """



    def __init__(self, api_url: str = "", timeout: int = PROVIDER_DEFAULT_TIMEOUT):

        self._timeout = timeout  # overall provider call budget (config.PROVIDER_DEFAULT_TIMEOUT)



        # Config shortcuts for split modules (single source of truth: CLIENT/client_config)

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
        """True while the CLIENT is configured for web-relay mode (live)."""

        return _relay_mode()

    def _build_ssl_context(self):
        """SSL context for RELAY mode - fail-closed.

        The relay is internet-facing, so the client->relay link must be trusted: either full CA
        verification (WEB_RELAY_VERIFY_SSL_CERTS) or a usable cert pin
        (WEB_RELAY_CERT_PIN_FINGERPRINT). There is no CERT_NONE fallback. Returns the context, or
        None when TLS cannot be trusted - callers must then refuse to connect (see _relay_connect()).
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

        Direct mode: one per address from client_config.get_server_address_list() (local first).
        Relay mode: the relay alone. Read fresh on every call so settings.json edits apply to the
        very next connection.
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

        from client_config import get_server_address_list, get_good_server
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

        # enable_thinking: bool or canonical level ("none"/"low"/"medium"/"high"),
        # forwarded verbatim; the SERVER translates it per model.
        temperature: float = 0.7, enable_thinking: Any = False,

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
            """Provider health probe (async).

            Runs on the caller's event loop, so no thread bridge is needed here.
            """
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

            Awaits the relay/direct implementation on the caller's event loop.
            """
            try:
                return await asyncio.wait_for(self._async_get_models(), timeout=30)
            except TimeoutError:
                # Bounded wait expired - the caller (agent router /api/models) decides how to log it.
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

            Awaits the relay/direct implementation on the caller's event loop.
            """
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

            The _model_switch_in_progress flag is owned by the agent router. Awaits the relay/direct
            implementation on the caller's event loop, bounded by MODEL_SWITCH_CLIENT_BUDGET_SEC; a
            budget expiry is reported as a clean failure.
            """
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

                      temperature: float = 0.7, enable_thinking: Any = False,

                      tools: list = None) -> Dict:

        """Build the request payload (minimal - the SERVER shapes the final request).

        enable_thinking is a bool or a canonical level; the SERVER translates a level into the
        model's own template kwargs.
        """

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
        """Transcribe an image LOCALLY on this CLIENT (never on the SERVER).

        The CLIENT runs its own dedicated llama-server (GLM-OCR + mmproj) on a side port;
        the llama files are provisioned from the SERVER when missing. *ocr_models* is kept
        for signature compatibility but ignored: local OCR always uses the GLM-OCR model.
        """
        from .. import ocr_files, ocr_local

        # 1. Provision llama-server.exe + DLLs + GLM-OCR + mmproj from the SERVER
        #    when this CLIENT does not already hold them hash-verified.
        try:
            ok, msg = await ocr_files.provision_ocr_files(self)
        except Exception as e:
            logger.error("[CLIENT OCR] file provisioning failed: %s", e)
            return f"ERROR: OCR file provisioning failed: {e}"
        if not ok:
            return f"ERROR: {msg}"

        # 2. Launch / reuse the local OCR llama-server (blocking subprocess -> worker thread).
        ok, base_url = await asyncio.to_thread(ocr_local.ensure_local_ocr_server)
        if not ok:
            return f"ERROR: {base_url}"

        # 3. Transcribe locally (blocking HTTP POST -> worker thread), then STOP the server.
        #    Lifecycle is strictly: start -> use -> stop when done (never left warm).
        try:
            text = await asyncio.to_thread(ocr_local.transcribe_local, base_url, image_base64)
        except Exception as e:
            return f"ERROR: local OCR failed: {e}"
        finally:
            try:
                await asyncio.to_thread(ocr_local.shutdown_local_ocr_server)
            except Exception as e:
                logger.warning("[CLIENT OCR] post-use shutdown failed: %s", e)
        return text

    async def tokenize(self, text: str) -> Optional[int]:
        """Exact token count of *text* via the SERVER (llama.cpp /tokenize).

        The CLIENT has no tokenizer of its own, so it asks the SERVER to count the
        string exactly. Returns the token count, or None when the tokenizer is
        unavailable (deepseek backend / llama down); callers fall back to chars/4 only
        on None. Used for the tool-schema cost so trim budgets reserve the real number.
        """
        if not text:
            return 0
        resp = await self.send_tool_request("tokenize_request", content=text, recv_timeout=15.0)
        if not resp or resp.get("type") != "tokenize_response":
            return None
        t = resp.get("tokens")
        return int(t) if isinstance(t, int) else None

    def start_server(self) -> bool:

        """Always succeeds - there is nothing local to start; we connect out."""

        logger.info("[CLIENT] CoolemsClient - no local server needed (connecting to remote)")

        return True
