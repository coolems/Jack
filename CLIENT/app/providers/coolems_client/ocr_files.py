"""Client-side OCR llama-file provisioning (SERVER pushes the files).

The SERVER provisions the llama files this CLIENT needs to run OCR locally
(llama-server.exe + DLLs + GLM-OCR model + mmproj). The exchange is SERVER-DRIVEN:

    1. The client sends ONE bare ocr_provision_request (no inventory).
    2. The SERVER answers with the FULL list of files it has
       (ocr_provision_manifest: files).
    3. The client compares the list with what it holds:
         - files it has that the SERVER does not list  -> removed locally;
         - files it already holds, verified            -> it replies ocr_file_skip;
         - files missing / different / unverified      -> it replies ocr_file_ready.
    4. The SERVER streams only the "ready" files, one by one (ocr_file_chunk frames +
       ocr_file_done); each file lands in <CLIENT>/providers/llama/ (mirroring the SERVER
       layout), is size + sha256 verified, and the client reports the result back
       (ocr_file_ack ok=true/false) so the SERVER can resend that file or move on.

TRANSPORT: everything travels on ONE short-lived WebSocket to the same wss:// endpoint
and PORT the client already uses for chat (direct: <server>:8080/ws/client, or the web
relay). No extra port is ever opened or listened on, so a firewall that already allows
the client's normal traffic allows provisioning too. The link is the same
authenticated + TLS-encrypted channel: the auth frame carries the client's API key,
and file payloads are base64 chunks in the same JSON frames.

All network I/O is async (websockets text frames, base64 chunks); disk writes are
plain synchronous file ops — fine at this scale and never on a hot path.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid

from client_config import PROTOCOL_VERSION

logger = logging.getLogger("COOLEMS.Provider.CoolemsClient.OCRFiles")

# One file's payload can take minutes on a slow link; each FRAME must not.
RECV_TIMEOUT = 300
# Must match the SERVER's limit: after this many resends the session is aborted.
MAX_RESENDS = 3
# The SERVER builds the manifest by hashing every (large) file the first time it is
# asked after a (re)start; on a slow/busy disk that can exceed 30s, so allow a minute.
MANIFEST_TIMEOUT = 60


class _FatalServerAbort(Exception):
    """The SERVER ended this provisioning session - stop retrying immediately."""


def client_llama_root() -> str:
    """Absolute path of <CLIENT>/providers/llama/ (the local OCR llama home, relative to CLIENT)."""
    here = os.path.dirname(os.path.abspath(__file__))          # .../coolems_client
    client_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))  # CLIENT
    return os.path.join(client_root, "providers", "llama")


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# abspath -> (size, mtime_ns, sha256): the OCR binaries + models are ~2 GB, so a file that
# was already hash-verified is not re-hashed on the next transcription (files persist on the
# CLIENT for later use; only a changed size/mtime re-verifies).
_verified: dict = {}


def _local_state(root: str) -> dict:
    """name (posix, relative to root) -> (size, mtime_ns, sha256) for every local file."""
    state = {}
    if not os.path.isdir(root):
        return state
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            path = os.path.join(dirpath, fn)
            if path.endswith(".part"):
                continue
            try:
                st = os.stat(path)
                key = os.path.abspath(path)
                cached = _verified.get(key)
                if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
                    digest = cached[2]
                else:
                    digest = _sha256_of(path)
                    _verified[key] = (st.st_size, st.st_mtime_ns, digest)
                name = os.path.relpath(path, root).replace(os.sep, "/")
                state[name] = (st.st_size, st.st_mtime_ns, digest)
            except OSError:
                continue
    return state


def _remove_stale(root: str, manifest_names: set) -> int:
    """Delete local files the SERVER no longer lists (the manifest is the source of truth)."""
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            name = os.path.relpath(path, root).replace(os.sep, "/")
            if name not in manifest_names:
                try:
                    os.remove(path)
                    _verified.pop(os.path.abspath(path), None)
                    removed += 1
                    logger.info("[OCRFiles] removed stale local file: %s", name)
                except OSError as e:
                    logger.warning("[OCRFiles] could not remove stale %s: %s", name, e)
    while True:
        # drop now-empty subdirectories (providers/llama/ itself is kept)
        empty = []
        for dirpath, dirnames, _filenames in os.walk(root, topdown=False):
            if dirpath != root and not os.listdir(dirpath):
                empty.append(dirpath)
        if not empty:
            break
        for d in empty:
            try:
                os.rmdir(d)
            except OSError:
                pass
    return removed


async def _authenticated_ws(provider):
    """Open + authenticate ONE WS to the SERVER on the client's normal endpoint/port."""
    from . import open_with_failover, _check_protocol

    candidates = provider._get_ws_candidates()
    ws, _url = await open_with_failover(
        candidates, max_size=provider.max_msg_size,
        ping_interval=provider.heartbeat_interval, ping_timeout=provider.ping_timeout,
    )
    try:
        api_key = provider._load_api_key()
        await ws.send(json.dumps({"type": "auth", "api_key": api_key, "role_type": "client",
                                  "protocol_version": PROTOCOL_VERSION}))
        auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))
        if auth_response.get("type") != "auth_ok":
            raise ConnectionError(f"OCR file auth failed: {auth_response}")
        _check_protocol(auth_response, "ocr files")
        return ws
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
        raise


def _save_chunk(entry: dict, dest: str, name: str, offset: int, data_b64: str) -> None:
    """Append one chunk to the .part file, at the expected offset (gap = corrupt stream)."""
    tmp = dest + ".part"
    with open(tmp, "ab") as f:
        expected = f.tell()
        if expected != offset:
            raise RuntimeError(f"{name}: chunk gap (expected offset {expected}, got {offset})")
        f.write(base64.b64decode(data_b64))


def _finish_file(entry: dict, dest: str) -> None:
    """Verify the .part file (size + sha256) and atomically rename it into place."""
    name = entry["name"]
    tmp = dest + ".part"
    if os.path.getsize(tmp) != int(entry["size"]):
        os.remove(tmp)
        raise RuntimeError(f"{name}: size mismatch after download")
    if _sha256_of(tmp) != entry["sha256"]:
        os.remove(tmp)
        raise RuntimeError(f"{name}: sha256 mismatch - discarded")
    os.replace(tmp, dest)
    st = os.stat(dest)
    _verified[os.path.abspath(dest)] = (st.st_size, st.st_mtime_ns, entry["sha256"])


async def _receive_file(ws, entry: dict, dest: str) -> None:
    """Receive one SERVER-streamed file (chunk frames until ocr_file_done), verify, rename."""
    name = entry["name"]
    tmp = dest + ".part"
    try:
        with open(tmp, "wb") as _f:  # fresh file; also proves the path is writable
            pass
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "ocr_file_chunk":
                if msg.get("name") != name:
                    raise RuntimeError(f"{name}: chunk for another file {msg.get('name')!r}")
                _save_chunk(entry, dest, name, msg.get("offset", 0), msg.get("data", ""))
            elif t == "ocr_file_done":
                break
            elif t in ("ocr_file_error", "error"):
                # The SERVER ended the session (unknown file / disk error / resends
                # exhausted): fatal, not retryable - do not wait for more chunks.
                raise _FatalServerAbort(msg.get("message", "server aborted the file"))
            else:
                logger.debug("[OCRFiles] ignoring frame type=%s during %s", t, name)
        _finish_file(entry, dest)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


async def provision_ocr_files(provider):
    """SERVER-driven OCR file provisioning over ONE authenticated WS.

    Sends the bare request, receives the SERVER's full file list, removes local files
    the SERVER no longer lists, then per file replies skip (already verified here) or
    ready (receive it). Each received file is verified and reported back (ok/not-ok) so
    the SERVER can resend it or move on. Returns (ok, message); never raises.
    """
    root = client_llama_root()
    os.makedirs(root, exist_ok=True)

    ws = await _authenticated_ws(provider)
    # Session id tags this client's OCR frames so the relay can tell them apart from
    # the SAME client's frames on its other (chat) connection.
    session_id = uuid.uuid4().hex
    try:
        # 1. The client just asks for files.
        await ws.send(json.dumps({"type": "ocr_provision_request", "session_id": session_id}))

        # 2. The SERVER tells the client the full list of files it has.
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=MANIFEST_TIMEOUT))
        except Exception as e:
            return False, f"SERVER did not answer the OCR provision request: {e}"
        if not isinstance(msg, dict):
            return False, f"SERVER sent an invalid OCR response: {msg!r}"
        if msg.get("type") in ("ocr_file_error", "error"):
            return False, f"SERVER refused OCR provisioning: {msg.get('message', 'unknown reason')}"
        if msg.get("type") != "ocr_provision_manifest":
            return False, f"SERVER did not provide an OCR file manifest: {msg}"
        files = msg.get("files", [])
        if not files:
            return False, "SERVER has no OCR llama files to provision"

        # 3. Compare with what the client holds: extra local files are removed.
        local = _local_state(root)
        manifest_names = {e["name"] for e in files}
        _remove_stale(root, manifest_names)

        # 4. Per file: skip what is already verified here, ready for the rest.
        to_receive = 0
        for e in files:
            name = e["name"]
            dest = os.path.join(root, name.replace("/", os.sep))
            state = local.get(name)
            if (state is not None and state[0] == e["size"]
                    and state[2] == e["sha256"] and os.path.isfile(dest)):
                await ws.send(json.dumps({"type": "ocr_file_skip", "name": name,
                                          "session_id": session_id}))
                continue
            to_receive += 1
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            await ws.send(json.dumps({"type": "ocr_file_ready", "name": name,
                                      "session_id": session_id}))
            resends = 0
            while True:
                try:
                    await _receive_file(ws, e, dest)
                except _FatalServerAbort as err:
                    return False, f"OCR file {name}: {err}"
                except Exception as err:
                    # Verification failure, a chunk timeout, or a dropped link all land here:
                    # report not-ok so the SERVER re-sends this file (up to MAX_RESENDS).
                    resends += 1
                    if resends > MAX_RESENDS:
                        return False, f"OCR file {name} failed after {MAX_RESENDS} resends: {err}"
                    logger.warning("[OCRFiles] %s - reporting not-ok, SERVER will resend (attempt %d)", err, resends)
                    try:
                        await ws.send(json.dumps({"type": "ocr_file_ack", "name": name, "ok": False,
                                                  "message": str(err), "session_id": session_id}))
                    except Exception as send_err:
                        return False, f"OCR file {name} failed and the link is gone: {send_err}"
                    continue
                await ws.send(json.dumps({"type": "ocr_file_ack", "name": name, "ok": True,
                                          "session_id": session_id}))
                logger.info("[OCRFiles] received + verified %s (%d bytes)", name, e["size"])
                break

        # 5. The SERVER confirms the session is complete.
        try:
            done = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if done.get("type") != "ocr_provision_done":
                logger.warning("[OCRFiles] expected ocr_provision_done, got %r", done.get("type"))
        except Exception as e:
            logger.warning("[OCRFiles] no ocr_provision_done from SERVER: %s", e)
        logger.info("[OCRFiles] provisioning complete: %d file(s) received, %d skipped",
                    to_receive, len(files) - to_receive)
        return True, ""
    finally:
        try:
            await ws.close()
        except Exception:
            pass
