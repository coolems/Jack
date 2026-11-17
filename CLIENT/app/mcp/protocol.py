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
from typing import Any, Dict, List, Optional

logger = logging.getLogger("COOLEMS.MCP.Protocol")

#: A single MCP frame is one JSON object on one line. Generous, but bounded: an endless
#: line from a misbehaving server must not grow memory without limit.
MAX_FRAME_BYTES = 8 * 1024 * 1024
#: Diagnostics kept from a server's stderr (the model never sees it; the log/UI does).
STDERR_KEPT_BYTES = 4096
#: Bytes the stdio reader pulls per raw read. read1() returns what is available (at most
#: this), so a small frame is delivered immediately instead of waiting for a full buffer.
READ_CHUNK_BYTES = 64 * 1024
#: Frames the reader thread may hand to the event loop before the loop falls behind. A
#: server that streams frames faster than they can be consumed is stopped (transport
#: failure) instead of being allowed to grow an unbounded queue in this process.
READER_QUEUE_MAX = 64
#: Total bytes allowed to sit in that queue. A frame is capped at MAX_FRAME_BYTES, so the
#: count alone would still permit 64 x 8 MB of held payloads.
READER_QUEUE_BYTES_MAX = 16 * 1024 * 1024
#: Wall clock for one pipe write. A server that stops reading fills the OS pipe and then
#: blocks the writer; the write runs off the loop and is abandoned (transport closed) after
#: this, so one stalled child cannot hold a chat turn for minutes.
WRITE_TIMEOUT_SEC = 10.0


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
        # Mark the exception as retrieved: the request coroutine may already be unwinding
        # with its own error, and an unawaited future would log
        # "Future exception was never retrieved" at GC time. A later await still raises it.
        p.future.exception()

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
        # Handoff accounting (also set in start()); initialised here so a transport that is
        # never started cannot raise inside _offer().
        self._queued_items = 0
        self._queued_bytes = 0
        self._write_lock = threading.Lock()
        #: Serializes pipe writes (one request at a time) without blocking the loop.
        self._write_gate = asyncio.Lock()

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
        self._queued_items = 0
        self._queued_bytes = 0
        # The loop-side reader task consumes the queue; the daemon thread owns the blocking
        # pipe reads. Splitting them keeps the event loop free while the server thinks and
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

    def _offer(self, line: bytes) -> bool:
        """Hand one line to the event loop, bounded. False when the loop is not keeping up.

        A server that streams frames faster than they are consumed would otherwise grow an
        unbounded asyncio.Queue in this process (a frame is capped, but 64 x 8 MB of them is
        not). Refusing the handoff stops the transport with a clear reason instead.
        """
        if (self._queued_items >= READER_QUEUE_MAX
                or self._queued_bytes >= READER_QUEUE_BYTES_MAX):
            return False
        self._queued_items += 1
        self._queued_bytes += len(line)
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, line)
        except Exception:
            return False
        return True

    def _pump_stdout(self) -> None:
        """Daemon thread: hand every line to the loop, EOF as an empty bytes marker.

        read1() instead of readline(): readline() has no size limit, so a server writing one
        endless line put the WHOLE line in RAM before _decode() could reject it - the
        MAX_FRAME_BYTES ceiling then limited what was PARSED, not what was allocated (64 MB
        measured for an 8 MB cap). Accumulating raw chunks bounds the buffer at the ceiling
        and hands over just enough bytes for _decode() to report the violation.
        """
        proc = self._proc
        loop = self._loop
        buf = bytearray()

        def eof():
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, b"")
            except Exception:
                pass

        def overflow():
            # The loop is not consuming frames as fast as this server writes them. Report it
            # through the queue so the reason reaches _fail_all instead of a silent stall.
            try:
                loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    JsonRpcError(f"server sent frames faster than they could be read "
                                 f"(queue limit {READER_QUEUE_MAX} frame(s) / "
                                 f"{READER_QUEUE_BYTES_MAX} bytes)"))
            except Exception:
                pass

        try:
            while proc is not None and proc.stdout is not None:
                chunk = proc.stdout.read1(READ_CHUNK_BYTES)
                if not chunk:
                    # EOF: hand over every complete line still buffered, then the trailing
                    # remainder (a final frame written without a newline). Dumping the whole
                    # buffer as ONE line would glue frames together into a non-JSON frame.
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line, buf = bytes(buf[:nl + 1]), buf[nl + 1:]
                        if not self._offer(line):
                            overflow()
                            return
                    if buf and not self._offer(bytes(buf)):
                        overflow()
                        return
                    eof()
                    return
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line, buf = bytes(buf[:nl + 1]), buf[nl + 1:]
                    if not self._offer(line):
                        overflow()
                        return
                if len(buf) > MAX_FRAME_BYTES:
                    # Oversized frame: hand over ceiling+1 so the reader reports it, then stop
                    # reading - the transport is dead and the child will be terminated.
                    if not self._offer(bytes(buf[:MAX_FRAME_BYTES + 1])):
                        # The loop is not consuming frames, so this handoff (and with it the
                        # oversized report) would vanish: the reader task waits forever and
                        # every request only ever hits its timeout ("did not answer"), with a
                        # dead reader and a transport still marked alive. Report the reason.
                        overflow()
                    return
        except Exception:
            # A closed/broken pipe is reported through the queue as EOF, not as a thread crash.
            eof()

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
            if isinstance(line, JsonRpcError):
                # The reader thread's overflow marker: a reason, not a frame.
                self._fail_all(str(line))
                return
            self._queued_items = max(0, self._queued_items - 1)
            self._queued_bytes = max(0, self._queued_bytes - len(line))
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
        async with self._write_gate:
            # One request at a time, and the blocking pipe write runs in a DAEMON thread -
            # not to_thread(): an executor thread is joined at interpreter exit, so a child
            # that never reads stdin would hang the process on shutdown.
            loop = asyncio.get_running_loop()
            done = loop.create_future()

            def _finish(err):
                if not done.done():
                    if err is not None:
                        done.set_exception(err)
                        done.exception()   # retrieved: no GC-time warning if nobody awaits
                    else:
                        done.set_result(None)

            def worker():
                err = None
                try:
                    with self._write_lock:
                        proc.stdin.write(data)
                        proc.stdin.flush()
                except Exception as e:      # OSError/ValueError from a closed or full pipe
                    err = e
                try:
                    loop.call_soon_threadsafe(_finish, err)
                except Exception:
                    pass

            threading.Thread(target=worker, daemon=True, name=f"mcp-stdin-{self.name}").start()
            try:
                await asyncio.wait_for(done, timeout=WRITE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                # The OS pipe is full because the server stopped reading. Abandon it: the
                # child is closed by close(), which unblocks the thread with a broken pipe.
                self._fail_all(f"stdin write did not finish within {int(WRITE_TIMEOUT_SEC)}s "
                               "(the server is not reading)")
                raise JsonRpcError(
                    f"MCP server '{self.name}' did not read the request within "
                    f"{int(WRITE_TIMEOUT_SEC)}s - it is stalled. Do not retry it.")
            except Exception as e:
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
    """JSON-RPC over HTTP POST (a streamable MCP endpoint). SSE frames are parsed.

    SECURITY: every outbound request goes through the SSRF gate (app.mcp.net_guard, which
    delegates to tools/ssrf_guard - the same policy the browser tools enforce). A private /
    loopback / link-local endpoint is refused here until the human approves that exact URL,
    so a config entry can never reach an internal address on its own. needs_consent reports
    why the connection was refused, so the registry can ask the card instead of failing."""

    def __init__(self, url: str, *, name: str = "http", timeout: float = 30.0,
                 headers: Optional[Dict[str, str]] = None):
        super().__init__(name)
        self.url = url
        self.timeout = timeout
        self.session_id = ""
        self._client = None
        self._headers = dict(headers or {})
        self.needs_consent = False
        self.consent_reason = ""

    def gate(self):
        """(ok, consentable, reason) for self.url. Fail-closed: never raises."""
        from .net_guard import endpoint_gate
        return endpoint_gate(self.url)

    async def _gate_async(self):
        """gate() with DNS off the event loop.

        endpoint_gate() resolves a hostname synchronously (socket.getaddrinfo). It is
        consulted before EVERY request, so on the loop thread one slow or timing-out
        lookup would freeze the whole UI - the same reason tools run via to_thread.
        IP literals need no DNS and stay on the loop."""
        from .net_guard import needs_dns_lookup
        if needs_dns_lookup(self.url):
            return await asyncio.to_thread(self.gate)
        return self.gate()

    async def start(self) -> None:
        ok, consentable, reason = await self._gate_async()
        if not ok:
            self.needs_consent = bool(consentable)
            self.consent_reason = reason or "endpoint refused"
            raise JsonRpcError(
                f"MCP server '{self.name}' endpoint refused: {reason}"
                + (" - it needs your explicit approval (consent card)" if consentable else
                   " - this target cannot be approved"))
        import httpx
        # follow_redirects=False: a redirect could land on a private address the gate never
        # checked. A 3xx is reported as a failed request instead of being followed.
        self._client = httpx.AsyncClient(timeout=self.timeout, headers=self._headers,
                                         follow_redirects=False)

    async def _check_gate(self) -> None:
        """Re-check before EVERY request: an approval is per exact URL, and a redirect or a
        changed target must not slip past the decision the user made. DNS runs off the loop
        (see _gate_async) so a slow lookup cannot freeze the UI mid-turn."""
        ok, consentable, reason = await self._gate_async()
        if not ok:
            self.needs_consent = bool(consentable)
            self.consent_reason = reason or "endpoint refused"
            raise JsonRpcError(f"MCP server '{self.name}' endpoint refused: {reason}")

    async def _write(self, data: bytes) -> None:
        raise NotImplementedError  # HTTP is request-shaped, not stream-shaped

    async def _read_body(self, resp, deadline: float, method: str, timeout: float) -> bytes:
        """Read a streamed response body under a TOTAL deadline.

        httpx's client timeout bounds each individual socket operation, not the whole body,
        so an endpoint that drips a few bytes every few seconds kept the request alive
        forever (measured: 9.17s of body for a caller that asked 2s - and during CLIENT
        startup that delay sits inside startup_event, so the UI is not served until it ends).
        The deadline is checked per chunk, so the same timeout_sec the stdio path honours is
        honoured here too."""
        loop = asyncio.get_running_loop()
        ait = resp.aiter_bytes().__aiter__()
        buf = bytearray()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise JsonRpcError(
                    f"MCP server '{self.name}' sent no complete answer to '{method}' "
                    f"within {int(timeout)}s")
            try:
                chunk = await asyncio.wait_for(ait.__anext__(), timeout=remaining)
            except asyncio.TimeoutError:
                raise JsonRpcError(
                    f"MCP server '{self.name}' sent no complete answer to '{method}' "
                    f"within {int(timeout)}s")
            except StopAsyncIteration:
                break
            buf.extend(chunk)
            if len(buf) > MAX_FRAME_BYTES:
                raise JsonRpcError(
                    f"MCP server '{self.name}' sent more than {MAX_FRAME_BYTES} bytes")
        return bytes(buf)

    async def request(self, method: str, params: Dict[str, Any], timeout: float) -> Any:
        if self._client is None:
            raise JsonRpcError(f"MCP server '{self.name}' is not connected")
        await self._check_gate()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        rid = self._new_id()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            # Streamed + capped: a hostile endpoint must not be able to make us buffer an
            # unbounded body (MAX_FRAME_BYTES is the same ceiling the stdio reader uses).
            async with self._client.stream("POST", self.url,
                                           content=_encode({"jsonrpc": "2.0", "id": rid,
                                                            "method": method,
                                                            "params": params or {}}),
                                           headers=headers) as resp:
                sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                if resp.status_code >= 400:
                    raise JsonRpcError(
                        f"MCP server '{self.name}' returned HTTP {resp.status_code}")
                ctype = (resp.headers.get("content-type") or "").lower()
                body = (await self._read_body(resp, deadline, method, timeout)).decode(
                    "utf-8", errors="replace")
        except JsonRpcError:
            raise
        except Exception as e:
            raise JsonRpcError(f"MCP server '{self.name}' request failed: {e}")
        for obj in self._frames(ctype, body):
            if obj.get("id") is not None and str(obj.get("id")) == rid:
                if "error" in obj:
                    err = obj.get("error") or {}
                    raise JsonRpcError(str(err.get("message") or "server error"),
                                       int(err.get("code") or -1), err.get("data"))
                return obj.get("result")
        raise JsonRpcError(f"MCP server '{self.name}' sent no response to '{method}'")

    def _frames(self, ctype: str, body: str) -> List[Dict[str, Any]]:
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
            await self._check_gate()
        except JsonRpcError:
            return
        try:
            # The response of a notification is meaningless: stream it and let the context
            # close the body instead of buffering whatever the server sends back. Bounded by
            # self.timeout - connect() notifies during CLIENT startup, and an endpoint that
            # keeps the connection open would otherwise hold startup_event forever.
            deadline = asyncio.get_running_loop().time() + self.timeout
            async with self._client.stream("POST", self.url, content=_encode(
                    {"jsonrpc": "2.0", "method": method, "params": params or {}}),
                    headers={"Content-Type": "application/json"}) as resp:
                ait = resp.aiter_bytes().__aiter__()
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(ait.__anext__(), timeout=remaining)
                    except (asyncio.TimeoutError, StopAsyncIteration):
                        break
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
