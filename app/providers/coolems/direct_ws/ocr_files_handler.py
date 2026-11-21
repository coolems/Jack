"""Server-driven OCR file provisioning (SERVER side).

The SERVER provisions a CLIENT-local OCR llama-server (llama-server.exe + DLLs +
GLM-OCR model + mmproj) by PUSHING the files it needs. The CLIENT sends one bare
request; the SERVER answers with the FULL list of files it has; the client then
decides, file by file, whether it already holds it (skip) or is ready to receive
it; the SERVER streams only the files it was told to send, one by one. After each
file the client reports ok/not-ok so the SERVER can resend that file or move on.

Wire format (text frames, JSON — same as every other COOLEMS message):

    CLIENT -> {"type": "ocr_provision_request", "session_id"}
    SERVER -> {"type": "ocr_provision_manifest", "files": [{"name", "size", "sha256"}, ...]}
    per file, in manifest order:
    CLIENT -> {"type": "ocr_file_skip",  "name", "session_id"}    (client already has it)
    CLIENT -> {"type": "ocr_file_ready", "name", "session_id"}    (client will receive it)
    SERVER -> {"type": "ocr_file_chunk", "name", "offset", "data" (b64), "total_size"}  (0..N)
    SERVER -> {"type": "ocr_file_done", "name", "total_size"}
    CLIENT -> {"type": "ocr_file_ack", "name", "ok": true|false, "message"?, "session_id"}
    SERVER -> (not-ok: re-streams that file, max 3 times; ok: next file)
    SERVER -> {"type": "ocr_provision_done"}
              (or {"type": "ocr_file_error", "name", "message"} on failure)
"""

import base64
import json
import logging
import os

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer.OCRFiles")

# Raw bytes per frame. base64 grows it by 4/3, still far below the 10 MiB WS message cap.
CHUNK_BYTES = 2 * 1024 * 1024
# How many times one file may be re-sent before the session is aborted.
MAX_RESENDS_PER_FILE = 3
# Seconds to wait for the client's skip/ready/ack frames (a 2.3 GB model takes minutes).
ACK_TIMEOUT = 300


async def _safe_send(ws, payload: dict) -> bool:
    """Send one JSON frame; swallow link-loss so a dead client can't tear the loop down."""
    try:
        await ws.send(json.dumps(payload))
        return True
    except Exception as e:
        logger.warning("[OCRFiles] link lost while sending %s: %s", payload.get("type"), e)
        return False


async def _recv_ocr_frame(ws, expect_name: str):
    """Receive the next JSON frame, or None on timeout / bad JSON / link loss.

    Only this session's frames reach us: in direct mode the socket is dedicated to the
    provisioning connection; behind the relay the adapter filters by client id AND
    session_id and requeues everything else. So a None here means THIS client stopped
    answering (or misbehaved).
    """
    import asyncio

    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError:  # pragma: no cover
        ConnectionClosed = OSError
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=ACK_TIMEOUT)
        return json.loads(raw)
    except (asyncio.TimeoutError, ConnectionClosed, OSError, ConnectionError,
            json.JSONDecodeError) as e:
        logger.error("[OCRFiles] no valid frame for %s: %s", expect_name, e)
        return None


async def _stream_ocr_file(ws, name: str) -> bool:
    """Stream one manifest file to the client in base64 chunk frames + done frame.

    Returns True when the file was fully sent; False when it could not be served or the
    link died mid-stream (the caller then aborts the session).
    """
    from ...llama.ocr_manifest import resolve_ocr_file

    path = resolve_ocr_file(name)
    if not path:
        logger.warning("[OCRFiles] Refused to stream unknown OCR file: %r", name)
        await _safe_send(ws, {"type": "ocr_file_error", "name": name,
                              "message": "unknown or disallowed OCR file"})
        return False

    try:
        size = os.path.getsize(path)
        offset = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_BYTES)
                if not chunk:
                    break
                if not await _safe_send(ws, {
                    "type": "ocr_file_chunk",
                    "name": name,
                    "offset": offset,
                    "data": base64.b64encode(chunk).decode("ascii"),
                    "total_size": size,
                }):
                    return False
                offset += len(chunk)
        if not await _safe_send(ws, {"type": "ocr_file_done", "name": name, "total_size": size}):
            return False
        logger.info("[SERVER->CLIENT] OCR file sent: %s (%d bytes)", name, size)
        return True
    except OSError as e:
        logger.error("[OCRFiles] Failed to stream %s: %s", name, e)
        await _safe_send(ws, {"type": "ocr_file_error", "name": name, "message": str(e)})
        return False


async def _handle_ocr_provision_request(ws, msg, user_info=None):
    """Send the full file list; then per file: wait skip/ready (once), stream, wait ok.

    Gated by profile: only a role with a non-empty 'ocr_model' may pull the OCR
    binaries + model. Fail-closed (None or not allowed -> refused before the manifest).
    """
    from app.auth_gateway.model_resolver import is_ocr_allowed
    from ...llama.ocr_manifest import build_ocr_manifest

    if not is_ocr_allowed(user_info):
        role = (user_info or {}).get("role", "unknown")
        logger.warning("[OCRFiles] OCR provision DENIED for role=%s (no 'ocr_model' rights)", role)
        await _safe_send(ws, {"type": "ocr_file_error", "name": "",
                              "message": f"OCR provisioning is not allowed for role '{role}'."})
        return

    manifest = build_ocr_manifest()
    if not manifest:
        await _safe_send(ws, {"type": "ocr_file_error", "name": "",
                              "message": "SERVER has no OCR llama files"})
        return

    # 1. The SERVER tells the CLIENT the full list of files it has.
    if not await _safe_send(ws, {"type": "ocr_provision_manifest", "files": manifest}):
        return
    logger.info("[SERVER->CLIENT] OCR manifest sent: %d file(s)", len(manifest))

    # 2. Per file: the CLIENT decides skip (already has it) or ready (receive it).
    for e in manifest:
        name = e["name"]

        # Phase A - wait for the client's skip/ready decision, exactly once.
        reply = await _recv_ocr_frame(ws, name)
        if reply is None:
            return
        t = reply.get("type")
        if t == "ocr_file_skip" and reply.get("name") == name:
            logger.info("[CLIENT->SERVER] OCR file skipped by client: %s", name)
            continue
        if t != "ocr_file_ready" or reply.get("name") != name:
            logger.error("[OCRFiles] Unexpected frame %r while waiting for skip/ready of %s", t, name)
            await _safe_send(ws, {"type": "ocr_file_error", "name": name,
                                  "message": f"unexpected frame: {t}"})
            return

        # Phase B - stream -> ack -> (resend | next), up to MAX_RESENDS_PER_FILE.
        resends = 0
        while True:
            if not await _stream_ocr_file(ws, name):
                return  # cannot serve, or the link died
            ack = await _recv_ocr_frame(ws, name)
            if ack is None:
                return
            if ack.get("type") == "ocr_file_ack" and ack.get("name") == name and ack.get("ok"):
                logger.info("[CLIENT->SERVER] OCR file ok: %s", name)
                break
            resends += 1
            if resends > MAX_RESENDS_PER_FILE:
                logger.error("[OCRFiles] %s not ok after %d resends - aborting", name, MAX_RESENDS_PER_FILE)
                await _safe_send(ws, {"type": "ocr_file_error", "name": name,
                                      "message": f"file failed after {MAX_RESENDS_PER_FILE} resends"})
                return
            logger.info("[CLIENT->SERVER] OCR file not ok: %s (%s) - resending (attempt %d)",
                        name, ack.get("message", "?"), resends)

    await _safe_send(ws, {"type": "ocr_provision_done"})
    logger.info("[SERVER->CLIENT] OCR provisioning complete")
