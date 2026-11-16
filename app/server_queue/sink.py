"""FrameSink - transport-agnostic delivery of response frames back to a chat.

Workers in the queue never know how the answer leaves: they push frames into a sink. The
sink is a WebSocket writer, a relay writer or a buffer, so the queue logic is identical
for every transport.
"""

import json
import logging

logger = logging.getLogger("COOLEMS.Provider.Queue.Sink")


class FrameSink:
    """Base sink. Subclasses implement _send_raw(); push_frame() handles JSON + errors."""

    @property
    def is_closed(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _send_raw(self, text: str):  # pragma: no cover - overridden
        raise NotImplementedError

    async def send_text(self, text: str) -> None:
        """Send a pre-serialized frame (the surface llama streaming uses)."""
        if self.is_closed:
            return
        try:
            await self._send_raw(text)
        except Exception as e:
            logger.debug("[SINK] send_text failed (treated as closed): %s", e)

    async def push_frame(self, frame: dict) -> None:
        """Serialize + send one JSON frame; never raises on a closed transport."""
        if self.is_closed:
            return
        try:
            await self._send_raw(json.dumps(frame))
        except Exception as e:
            logger.debug("[SINK] push_frame failed (treated as closed): %s", e)


class DirectWsSink(FrameSink):
    """Delivers frames to a direct WS client, reusing chat_relay's _WSAdapter."""

    def __init__(self, ws):
        from ..providers.coolems.chat_relay import _make_direct_ws_adapter
        self._adapter = _make_direct_ws_adapter(ws)

    @property
    def is_closed(self) -> bool:
        return self._adapter.is_closed

    async def _send_raw(self, text: str):
        await self._adapter.send_text(text)


class RelayTargetSink(FrameSink):
    """Delivers frames to ONE relayed client over the shared brain<->relay socket.

    Wraps chat_relay's relay adapter around ws_brain_relay's _RelayTargetAdapter, which
    tags every JSON frame with ``target_client`` (idempotent — already-tagged frames pass).
    """

    def __init__(self, ws, target_client: str):
        from ..providers.coolems.chat_relay import _make_relay_ws_adapter
        from ..providers.coolems.ws_brain_relay import _RelayTargetAdapter
        self._adapter = _make_relay_ws_adapter(_RelayTargetAdapter(ws, target_client))

    @property
    def is_closed(self) -> bool:
        return self._adapter.is_closed

    async def _send_raw(self, text: str):
        await self._adapter.send_text(text)
