"""Relayed OCR provisioning handler (ocr_provision_request).

A CLIENT running on another machine needs the llama-server files + GLM-OCR model to
run OCR locally. The provisioning is SERVER-DRIVEN: the client sends ONE bare
provisioning request, the SERVER answers with the full list of files it has, the
client replies per file (skip: already has it / ready: receive it), the SERVER
streams the ready files one by one, and the client reports ok/not-ok after each
file so the SERVER can resend it or move on.

This module runs that exchange when the client sits BEHIND the web relay: the
transfer rides the same encrypted brain<->relay socket as chat frames. Every
outgoing frame is tagged with target_client so the relay delivers it to exactly
one client; incoming frames from OTHER clients are requeued for the main loop.

Authorization runs against the RELAYED CLIENT's profile (fail-closed): the
binaries and models are server assets, never served to an unresolvable client.
"""

import json
import logging

from .relay_handlers import _relay_send, _resolve_relay_client_user_info

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")


class _RelayOCRAdapter:
    """WebSocket wrapper for one OCR provisioning session behind the relay.

    send(): tags every outgoing JSON frame with target_client (the brain socket is
    shared by N clients; the relay routes only tagged frames).
    recv(): returns the next frame FROM THIS client's OCR SESSION (matched by
    _relay_from AND the session_id the client generated); frames from other clients -
    and even this client's own frames on its other (chat) connection - are requeued
    (minus the brain-internal target_client tag) for the main message loop.
    """

    def __init__(self, ws, target_client: str, requeue, session_id: str):
        self._ws = ws
        self._target = target_client or ""
        self._requeue = requeue
        self._session_id = session_id or ""

    async def send(self, text):
        if self._target and isinstance(text, str) and text.lstrip().startswith("{"):
            try:
                frame = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                frame = None
            if isinstance(frame, dict) and "target_client" not in frame:
                frame["target_client"] = self._target
                text = json.dumps(frame)
        await self._ws.send(text)

    async def recv(self):
        while True:
            raw = await self._ws.recv()
            try:
                frame = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                frame = None
            if (isinstance(frame, dict) and self._session_id
                    and frame.get("_relay_from") == self._target
                    and frame.get("session_id") == self._session_id):
                return raw
            # Not this session's frame (another client, or this client's own chat
            # frame). Hand it back to the main loop untouched - exactly the bytes the
            # main loop would have received - so nothing is re-parsed differently.
            self._requeue.append(raw)
            logger.debug("[OCRFiles] requeued frame from relayed client %r",
                         frame.get("_relay_from") if isinstance(frame, dict) else "?")

    def __getattr__(self, name):
        # forward everything else (e.g. remote_address) to the real socket
        return getattr(self._ws, name)


async def _handle_relay_ocr_provision_request(ws, msg: dict, server_provider, requeue: list):
    """Run the server-driven OCR file push for one relayed client."""
    client_id = msg.get("_relay_from") or "unknown"
    session_id = msg.get("session_id") or ""
    if not session_id:
        logger.warning(f"[SERVER] Relay OCR provision DENIED: no session_id from client {client_id!r}")
        await _relay_send(ws, {
            "type": "ocr_file_error",
            "name": "",
            "message": "OCR provisioning requires a session_id.",
        }, client_id)
        return
    user_info = await _resolve_relay_client_user_info(ws, msg, server_provider)
    if not user_info:
        logger.warning(f"[SERVER] Relay OCR provision DENIED for unresolvable client {client_id!r} (fail-closed)")
        await _relay_send(ws, {
            "type": "ocr_file_error",
            "name": "",
            "message": f"OCR provisioning denied for relayed client {client_id!r} (unresolvable profile).",
        }, client_id)
        return

    from ..direct_ws.ocr_files_handler import _handle_ocr_provision_request
    await _handle_ocr_provision_request(_RelayOCRAdapter(ws, client_id, requeue, session_id), msg, user_info)
