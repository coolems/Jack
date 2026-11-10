"""MCP wire protocol — JSON-RPC 2.0 over stdio pipes or HTTP POST (2026-12).

Two transports, one interface:

    await t.send(method, params) -> result      (a request; raises JsonRpcError / TimeoutError)
    await t.notify(method, params)              (fire and forget; no response expected)
    await t.close()                             (never raises, never leaks a pipe/process)

A background reader owns the stream, so requests may run CONCURRENTLY (the loop dispatches
tool batches in parallel) and server notifications cannot block a response. Responses are
matched by id; anything that is not a response to a live request is a notification and is
kept out of the request path.

Oversized or undecodable frames become a JsonRpcError, never a crash: a broken server must
read as an honest tool error, not kill the chat turn.
"""

import asyncio
import json
import logging
import os
import subprocess
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("COOLEMS.MCP.Protocol")

#: A single MCP frame is one JSON object on one line. Generous, but bounded: an endless
#: line from a misbehaving server must not grow memory without limit.
MAX_FRAME_BYTES = 8 * 1024 * 1024
#: Diagnostics kept from a server's stderr (the model never sees it; the log/UI does).
STDERR_KEPT_BYTES = 4096


class JsonRpcError(Exception):
    """A protocol-level failure: an error object, a malformed frame, or a dead transport."""

    def __init__(self, message: str, code: int = -1, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def _encode(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _decode(line: bytes) -> Dict[str, Any]:
    if len(line) > MAX_FRAME_BYTES:
        raise JsonRpcError(f"server frame exceeded {MAX_FRAME_BYTES} bytes")
    try:
        obj = json.loads(line.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise JsonRpcError(f"server sent a non-JSON frame: {e}")
    if not isinstance(obj, dict):
        raise JsonRpcError("server frame is not a JSON object")
    return obj


class _Pending:
    __slots__ = ("future",)

    def __init__(self, future: asyncio.Future):
        self.future = future


class _Stream:
    """Shared request/response bookkeeping for both transports."""

    def __init__(self, name: str):
        self.name = name
        self._pending: Dict[str, _Pending] = {}
        self._dead: Optional[str] = None

    @property
    def alive(self) -> bool:
        return self._dead is None

    def _new_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _resolve(self, rid: str, result: Any) -> None:
        p = self._pending.pop(rid, None)
        if p is None or p.future.done():
            return
        p.future.set_result(result)

    def _reject(self, rid: str, err: JsonRpcError) -> None:
        p = self._pending.pop(rid, None)
        if p is None or p.future.done():
            return
        p.future.set_exception(err)

    def _fail_all(self, reason: str) -> None:
        self._dead = reason
        err = JsonRpcError(f"MCP server '{self.name}' transport closed: {reason}")
        for rid in list(self._pending):
            self._reject(rid, err)

    async def request(self, method: str, params: Dict[str, Any], timeout: float) -> Any:
        if not self.alive:
            raise JsonRpcError(f"MCP server '{self.name}' is not connected ({self._dead})")
        rid = self._new_id()
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut)
        try:
            await self._write(_encode({"jsonrpc": "2.0", "id": rid, "method": method,
                                       "params": params or {}}))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise JsonRpcError(
                f"MCP server '{self.name}' did not answer '{method}' within {int(timeout)}s")
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            raise
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        if not self.alive:
            return
        try:
            await self._write(_encode({"jsonrpc": "2.0", "method": method,
                                       "params": params or {}}))
        except Exception as e:  # a notification has no response; a dead pipe is reported later
            logger.debug("[MCP:%s] notify %s failed: %s", self.name, method, e)

    def _handle(self, obj: Dict[str, Any]) -> None:
        rid = obj.get("id")
        if rid is None:
            return  # a server notification (notifications/*, progress) - not our business
        key = str(rid)
        if key not in self._pending:
            return  # late/unknown response: drop it, never guess which request it belongs to
        if "error" in obj:
            err = obj.get("error") or {}
            self._reject(key, JsonRpcError(
                str(err.get("message") or "server error"),
                int(err.get("code") or -1), err.get("data")))
        else:
            self._resolve(key, obj.get("result"))

    async def _write(self, data: bytes) -> None:
        raise NotImplementedError


class StdioTransport(_Stream):
    """Newline-delimited JSON-RPC over a child process's stdin/stdout.

    The reader runs on a DAEMON thread, not a ThreadPoolExecutor: an executor's threads
    are joined at interpreter exit, so a server whose pipes are still open would hang the
    process forever (a failed test that never closed the transport showed exactly that).
    A daemon thread dies with the interpreter, and close() closes the pipes so it stops
    promptly in the normal path.
    """

    def __init__(self, command: str, args: List[str], *, env: Optional[Dict[str, str]] = None,
                 cwd: str = "", name: str = "stdio"):
        super().__init__(name)
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd or None
        self.stderr_text = ""
        self._proc: Optional[subprocess.Popen] = None
        self._loop = None
        self._queue: Optional[asyncio.Queue] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._reader_thread = None
        self._stderr_thread = None
        self._write_lock = threading.Lock()

    async def start(self) -> None:
        merged = dict()
        try:
            merged.update(dict(os.environ))
        except Exception:
            pass
        merged.update(self.env)
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.cwd, env=merged)
        except OSError as e:
            raise JsonRpcError(f"could not start MCP server '{self.name}': {e}")
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        # The loop-side reader task consumes the queue; the daemon thread owns the blocking
        # readline. Splitting them keeps the event loop free while the server thinks and
        # lets concurrent tool batches interleave requests.
        self._reader_task = asyncio.create_task(self._read_loop())
        self._reader_thread = threading.Thread(target=self._pump_stdout, daemon=True,
                                               name=f"mcp-stdout-{self.name}")
        self._reader_thread.start()
        # stderr is drained in the background so a chatty server never blocks on a full
        # pipe, and the bounded text is available for diagnostics after a failure.
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True,
                                               name=f"mcp-stderr-{self.name}")
        self._stderr_thread.start()

    def _pump_stdout(self) -> None:
        """Daemon thread: hand every line to the loop, EOF as an empty bytes marker."""
        proc = self._proc
        loop = self._loop
        try:
            while proc is not None and proc.stdout is not None:
                line = proc.stdout.readline()
                loop.call_soon_threadsafe(self._queue.put_nowait, line)
                if not line:
                    return
        except Exception:
            # A closed/broken pipe is reported through the queue as EOF, not as a thread crash.
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, b"")
            except Exception:
                pass

    def _pump_stderr(self) -> None:
        proc = self._proc
        try:
            while proc is not None and proc.stderr is not None:
                chunk = proc.stderr.read(1024)
                if not chunk:
                    return
                self.stderr_text = (self.stderr_text + chunk.decode("utf-8", "replace"))[-STDERR_KEPT_BYTES:]
        except Exception:
            return

    async def _read_loop(self) -> None:
        while True:
            try:
                line = await self._queue.get()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._fail_all(f"stdout read failed ({e})")
                return
            if not line:
                self._fail_all("stdout reached end of stream")
                return
            try:
                self._handle(_decode(line))
            except JsonRpcError as e:
                self._fail_all(str(e))
                return

    async def _write(self, data: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            self._fail_all("stdin is not open")
            raise JsonRpcError(f"MCP server '{self.name}' stdin is not open")
        # Serialized writes on the loop thread; a server that stops reading surfaces as
        # OSError from flush, which we turn into a clear transport failure.
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError) as e:
                self._fail_all(f"stdin write failed ({e})")
                raise JsonRpcError(f"MCP server '{self.name}' stdin failed: {e}")

    async def close(self) -> None:
        proc = self._proc
        self._fail_all("closed")
        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait({task}, timeout=2.0)
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass
            # Bounded poll instead of proc.wait() in a worker thread: an executor thread
            # blocked on wait() would be joined at interpreter exit and hang the process.
            for _ in range(30):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._proc = None
        self._reader_thread = None
        self._stderr_thread = None


class StreamableHttpTransport(_Stream):
    """JSON-RPC over HTTP POST (a streamable MCP endpoint). SSE frames are parsed."""

    def __init__(self, url: str, *, name: str = "http", timeout: float = 30.0,
                 headers: Optional[Dict[str, str]] = None):
        super().__init__(name)
        self.url = url
        self.timeout = timeout
        self.session_id = ""
        self._client = None
        self._headers = dict(headers or {})

    async def start(self) -> None:
        import httpx
        self._client = httpx.AsyncClient(timeout=self.timeout, headers=self._headers)

    async def _write(self, data: bytes) -> None:
        raise NotImplementedError  # HTTP is request-shaped, not stream-shaped

    async def request(self, method: str, params: Dict[str, Any], timeout: float) -> Any:
        if self._client is None:
            raise JsonRpcError(f"MCP server '{self.name}' is not connected")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        rid = self._new_id()
        try:
            resp = await self._client.post(self.url,
                                           content=_encode({"jsonrpc": "2.0", "id": rid,
                                                            "method": method,
                                                            "params": params or {}}),
                                           headers=headers)
        except Exception as e:
            raise JsonRpcError(f"MCP server '{self.name}' request failed: {e}")
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if resp.status_code >= 400:
            raise JsonRpcError(f"MCP server '{self.name}' returned HTTP {resp.status_code}")
        for obj in self._frames(resp):
            if obj.get("id") is not None and str(obj.get("id")) == rid:
                if "error" in obj:
                    err = obj.get("error") or {}
                    raise JsonRpcError(str(err.get("message") or "server error"),
                                       int(err.get("code") or -1), err.get("data"))
                return obj.get("result")
        raise JsonRpcError(f"MCP server '{self.name}' sent no response to '{method}'")

    def _frames(self, resp) -> List[Dict[str, Any]]:
        ctype = (resp.headers.get("content-type") or "").lower()
        body = resp.text
        if "text/event-stream" in ctype:
            out: List[Dict[str, Any]] = []
            for line in body.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
            return out
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            raise JsonRpcError(f"MCP server '{self.name}' sent a non-JSON body")
        return [obj] if isinstance(obj, dict) else (
            [o for o in obj if isinstance(o, dict)] if isinstance(obj, list) else [])

    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        if self._client is None:
            return
        try:
            await self._client.post(self.url, content=_encode(
                {"jsonrpc": "2.0", "method": method, "params": params or {}}),
                headers={"Content-Type": "application/json"})
        except Exception as e:
            logger.debug("[MCP:%s] notify %s failed: %s", self.name, method, e)

    async def close(self) -> None:
        self._fail_all("closed")
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


#: Factory used by client.py: one transport per config entry.
def make_transport(cfg, name: str) -> _Stream:
    if cfg.transport == "stdio":
        return StdioTransport(cfg.command, cfg.args, env=cfg.env, cwd=cfg.cwd, name=name)
    return StreamableHttpTransport(cfg.url, name=name, timeout=cfg.timeout_sec)
