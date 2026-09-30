"""Persistent authenticated WebSocket CONTROL CHANNEL for CoolemsClientProvider.

(2026-08-20, review item #6) Before this module existed, every health check / model
list / current-model call opened a FRESH WebSocket: TLS handshake + auth frame + one
request + close. Under load (model-switch polling, UI refreshes) that is pure churn.

This channel keeps ONE authenticated connection alive on the running event loop and
multiplexes request/response over it using req_id correlation:

    client -> {"type": "health_check",  "req_id": "<uuid4hex>", ...}
    server <- {"type": "health_ok",     "req_id": "<same uuid4hex>", ...}   (echoed)

Design notes / boundaries:
  - Used ONLY from async contexts on the uvicorn loop. Boot-time callers (before the
    loop exists) keep using the provider's sync methods, which intentionally use
    short-lived connections -- there is no running loop to host this channel in.
  - switch_model deliberately does NOT go through the channel: a model switch restarts
    the SERVER process, so any connection to it dies mid-switch by design. The two-phase
    poller (relay_mode/direct_mode) already handles that with fresh connections.
  - On ANY transport error the channel marks itself dead and closes; the next
    ensure() reconnects lazily. No background reconnection loop -- keep-alive is done
    by the websockets library's ping/pong, which surfaces dead peers as errors here.
"""

import asyncio
import json
import logging
import uuid

logger = logging.getLogger("COOLEMS.Provider.CoolemsClient.ControlChannel")


class ControlChannel:
    """One authenticated WS to SERVER, multiplexed by req_id. Not thread-safe (asyncio only)."""

    def __init__(self, provider):
        self._provider = provider
        self._ws = None
        self._reader_task = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()  # guards connect/disconnect races
        self._dead = False

    # --- lifecycle -----------------------------------------------------------

    async def ensure(self):
        """Connect + authenticate if needed. Returns True when a live channel exists."""
        async with self._lock:
            if self._ws is not None and not self._dead:
                return True
            await self._teardown_locked()  # clean any half-dead state first

            import websockets
            from . import _check_protocol, apply_auth_info

            try:
                # (2026-09-23 v4) in relay mode this raises ConnectionError when no trusted TLS
                # is configured (fail-closed) - handle it here so the channel degrades cleanly.
                candidates = self._provider._get_ws_candidates()
                api_key = self._provider._load_api_key()
            except Exception as e:
                logger.error(f"[CONTROL] Cannot start control channel: {e}")
                return False

            if not api_key:
                logger.error("[CONTROL] No API key available -- control channel cannot start")
                return False

            try:
                from . import open_with_failover
                ws, ws_url = await open_with_failover(
                    candidates, max_size=self._provider.max_msg_size,
                    ping_interval=self._provider.heartbeat_interval,
                    ping_timeout=self._provider.ping_timeout,
                )
            except (OSError, Exception) as e:
                logger.warning(f"[CONTROL] Connect failed ({type(e).__name__}: {e}) -- will retry on next call")
                return False

            try:
                # Auth once; protocol version negotiated like every other path.
                from config import PROTOCOL_VERSION
                await ws.send(json.dumps({
                    "type": "auth", "api_key": api_key, "role_type": "client",
                    "protocol_version": PROTOCOL_VERSION,
                }))
                auth_response = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=self._provider.handshake_timeout)
                )
                if auth_response.get("type") != "auth_ok":
                    logger.error(f"[CONTROL] Auth rejected: {auth_response}")
                    await ws.close()
                    return False
                _check_protocol(auth_response, "control channel")
                apply_auth_info(auth_response, "control channel")

                self._ws = ws
                self._dead = False
                self._reader_task = asyncio.create_task(self._read_loop(ws), name="coolems-control-reader")
                logger.info(f"[CONTROL] Channel established at {ws_url}")
                return True
            except Exception as e:
                logger.warning(f"[CONTROL] Handshake failed ({type(e).__name__}: {e})")
                try:
                    await ws.close()
                except Exception:
                    pass
                return False

    async def close(self):
        """Tear the channel down (app shutdown). Idempotent."""
        async with self._lock:
            await self._teardown_locked()

    async def _teardown_locked(self):
        ws, task = self._ws, self._reader_task
        self._ws = None
        self._reader_task = None
        self._dead = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Control channel closed"))
        self._pending.clear()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # --- reader / correlation --------------------------------------------------

    async def _read_loop(self, ws):
        """Dispatch every frame to the pending future that owns its req_id."""
        try:
            while True:
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("[CONTROL] Non-JSON frame on control channel -- ignoring")
                    continue

                req_id = msg.get("req_id")
                if not req_id:
                    # Unsolicited frame (shouldn't happen on this channel) -- log and drop.
                    logger.debug(f"[CONTROL] Frame without req_id dropped: type={msg.get('type')}")
                    continue

                fut = self._pending.pop(req_id, None)
                if fut is None or fut.done():
                    logger.warning(f"[CONTROL] Response for unknown/expired req_id={req_id[:8]}...")
                    continue
                fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Transport died -- mark dead; pending requests fail fast, next ensure() reconnects.
            self._dead = True
            logger.warning(f"[CONTROL] Reader loop ended ({type(e).__name__}: {e})")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError(f"Control channel lost: {type(e).__name__}"))
            self._pending.clear()

    # --- public API --------------------------------------------------------------

    async def request(self, req_type: str, timeout: float = 15.0, **kwargs):
        """Send one control request and await its correlated response.

        Returns the parsed response dict, or None on any failure (callers treat it
        exactly like a failed short-lived connection -- they already do).
        """
        if not await self.ensure():
            return None

        req_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        try:
            await self._ws.send(json.dumps({"type": req_type, "req_id": req_id, **kwargs}))
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            # A send failure means the transport is gone -- force reconnect next call.
            self._dead = True
            logger.warning(f"[CONTROL] Request {req_type} failed (req_id={req_id[:8]}...)")
            return None


# One channel per provider instance; created lazily on first async use.
_channel_registry: dict[int, ControlChannel] = {}


def get_control_channel(provider) -> ControlChannel:
    """Return the process-wide control channel for *provider* (created on demand)."""
    ch = _channel_registry.get(id(provider))
    if ch is None or ch._provider is not provider:
        ch = ControlChannel(provider)
        _channel_registry[id(provider)] = ch
    return ch
