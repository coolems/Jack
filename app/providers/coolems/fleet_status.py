"""Push live fleet health to attached WebSocket clients (2026-11 strata outage).

The registry fires its health-change listeners when a backend flips healthy/unhealthy and
on every health-poller heartbeat. This module subscribes one listener (wired in
server_provider._ensure_chat_queue) and, for each attached client, rebuilds the
profile-filtered model catalog and pushes a 'fleet_status' frame so an open outage dialog
can update in place instead of polling.
"""

import asyncio
import json
import logging

logger = logging.getLogger("COOLEMS.Provider.FleetStatus")


class FleetStatusHub:
    """Tracks attached direct-WS sockets and relay links for fleet_status pushes."""

    def __init__(self):
        self._direct: list = []      # [(ws, user_info), ...]
        self._relay_ws: list = []    # relay sockets (their clients live on ws._relay_client_keys)
        self._broadcasting = False
        self._rerun = False

    # --- subscription ----------------------------------------------------------

    def register_direct(self, ws, user_info) -> None:
        if not any(w is ws for w, _ in self._direct):
            self._direct.append((ws, user_info))

    def unregister_direct(self, ws) -> None:
        self._direct = [(w, u) for w, u in self._direct if w is not ws]

    def register_relay(self, ws) -> None:
        if not any(w is ws for w in self._relay_ws):
            self._relay_ws.append(ws)

    def unregister_relay(self, ws) -> None:
        self._relay_ws = [w for w in self._relay_ws if w is not ws]

    def has_subscribers(self) -> bool:
        return bool(self._direct) or bool(self._relay_ws)

    # --- broadcast --------------------------------------------------------------

    async def broadcast(self, server_provider, snapshot=None) -> None:
        """Push one 'fleet_status' frame to every subscriber (coalesced: concurrent
        requests run once, then once more if another arrived while broadcasting)."""
        if self._broadcasting:
            self._rerun = True
            return
        self._broadcasting = True
        try:
            while True:
                self._rerun = False
                await self._broadcast_once(server_provider, snapshot)
                if not self._rerun:
                    break
        finally:
            self._broadcasting = False

    async def _broadcast_once(self, server_provider, snapshot) -> None:
        if not self.has_subscribers():
            return
        from .model_catalog import build_model_catalog
        try:
            catalog = await build_model_catalog(server_provider, None)
        except Exception as e:  # pragma: no cover - a scan failure must not kill the poller
            logger.warning("[FLEET-STATUS] catalog build failed: %s", e)
            return

        for ws, user_info in list(self._direct):
            await self._safe_send(ws, {
                "type": "fleet_status",
                "models": self._allowed(server_provider, user_info, catalog),
            })

        if self._relay_ws:
            from .ws_brain_relay.relay_handlers import (_resolve_relay_client_user_info,
                                                        _relay_send)
            for ws in list(self._relay_ws):
                client_keys = dict(getattr(ws, "_relay_client_keys", None) or {})
                for cid in list(client_keys):
                    user_info = await _resolve_relay_client_user_info(
                        ws, {"_relay_from": cid}, server_provider)
                    if not user_info:
                        continue
                    await _relay_send(ws, {
                        "type": "fleet_status",
                        "models": self._allowed(server_provider, user_info, catalog),
                    }, cid)

    def _allowed(self, server_provider, user_info, catalog):
        try:
            return server_provider._auth_gateway.get_allowed_models(user_info, catalog)
        except Exception:
            return catalog

    async def _safe_send(self, ws, frame) -> None:
        try:
            await ws.send(json.dumps(frame))
        except Exception as e:
            logger.debug("[FLEET-STATUS] send failed: %s", e)


_hub: FleetStatusHub | None = None


def get_hub() -> FleetStatusHub:
    global _hub
    if _hub is None:
        _hub = FleetStatusHub()
    return _hub
