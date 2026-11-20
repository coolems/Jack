"""Tests for the Strata backend-outage resilience path.

Covers the typed recoverable error end-to-end plus the resume/switch wiring introduced for
the Strata OOM / engine-restart scenario:

  SERVER (tested in-process, the repo root owns the `app`/`config` packages):
    - BackendUnavailableError is a ConnectionError carrying the outage fields.
    - streaming._is_backend_unavailable flags 502/503/504 + OOM/restart markers, not 4xx.
    - chat_stream raises BackendUnavailableError on a 503 (mocked http client), and keeps a
      401 as a plain RuntimeError.
    - chat_relay.run_chat_core emits the tagged `backend_unavailable` recoverable frame.

  CLIENT (tested in a subprocess with CLIENT/ first on sys.path, because both sides ship
  their own `app` package and only one can own the name in a single process):
    - errors.raise_for_error_frame maps a recoverable frame to BackendUnavailableError and
      anything else to RuntimeError (shared by direct_mode / relay_mode).
    - backend_resume._switch_and_retry verifies routing via provider.switch_model and
      re-targets the preserved call; an unserved alternative fails cleanly.
    - backend_resume.resume_after_backend_outage re-sends the SAME in-flight messages.
"""

import asyncio
import json
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_CLIENT = os.path.join(_REPO, "CLIENT")


def _client_run(code: str):
    """Run *code* in a subprocess with CLIENT/ first on sys.path (isolated `app` package)."""
    env = {**os.environ}
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO, capture_output=True, text=True, timeout=90, env=env,
    )


# ---------------------------------------------------------------------------
# SERVER: typed error + detection
# ---------------------------------------------------------------------------

def test_backend_unavailable_error_is_connection_error():
    from app.providers.llama.errors import BackendUnavailableError
    err = BackendUnavailableError("down", model="m", retry_after_sec=30, oom=True, reason="raw")
    assert isinstance(err, ConnectionError)
    assert err.model == "m"
    assert err.retry_after_sec == 30
    assert err.oom is True
    assert err.reason == "raw"


def test_is_backend_unavailable_markers():
    from app.providers.llama.streaming import _is_backend_unavailable
    # Recoverable status codes regardless of body.
    assert _is_backend_unavailable(502, "") is True
    assert _is_backend_unavailable(503, "") is True
    assert _is_backend_unavailable(504, "") is True
    # Recoverable markers inside a body.
    assert _is_backend_unavailable(500, "engine exited before it was ready") is True
    assert _is_backend_unavailable(500, "no progress for 60 s") is True
    assert _is_backend_unavailable(500, "only 833 MiB free once the slots are written") is True
    assert _is_backend_unavailable(500, "loading the experts into RAM") is True
    # Permanent failures must NOT be treated as recoverable.
    assert _is_backend_unavailable(401, "") is False
    assert _is_backend_unavailable(404, "") is False
    assert _is_backend_unavailable(400, "bad request") is False
    assert _is_backend_unavailable(200, "") is False


# ---------------------------------------------------------------------------
# SERVER: streaming 503 -> typed error (mocked http client)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, body=b""):
        self.status_code = status_code
        self._body = body

    async def aread(self):
        return self._body


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """httpx stand-in: /health answers 200, the chat POST answers *status_code*."""

    def __init__(self, status_code, body=b""):
        self._status = status_code
        self._body = body

    async def get(self, url, **kwargs):
        return _FakeResponse(200)

    def stream(self, method, url, **kwargs):
        return _FakeStream(_FakeResponse(self._status, self._body))


def _patch_streaming_client(status_code, body):
    import app.providers.llama.streaming as streaming
    original = streaming.get_async_client

    async def fake_client():
        return _FakeClient(status_code, body)

    streaming.get_async_client = fake_client
    return original


def test_streaming_503_raises_backend_unavailable():
    from app.providers.llama.errors import BackendUnavailableError
    original = _patch_streaming_client(
        503, b'{"error":{"message":"only 833 MiB free once the slots are written"}}'
    )
    try:
        import app.providers.llama.streaming as streaming

        async def run():
            try:
                await streaming.chat_stream(
                    api_url="http://127.0.0.1:5000", timeout=30, model="test-model",
                    messages=[{"role": "user", "content": "hi"}], websocket=None,
                )
            except BackendUnavailableError as e:
                assert e.oom is True
                return "typed"
            return "not-raised"

        assert asyncio.run(run()) == "typed"
    finally:
        import app.providers.llama.streaming as streaming
        streaming.get_async_client = original


def test_streaming_401_stays_runtime_error():
    from app.providers.llama.errors import BackendUnavailableError
    original = _patch_streaming_client(401, b"unauthorized")
    try:
        import app.providers.llama.streaming as streaming

        async def run():
            try:
                await streaming.chat_stream(
                    api_url="http://127.0.0.1:5000", timeout=30, model="test-model",
                    messages=[{"role": "user", "content": "hi"}], websocket=None,
                )
            except BackendUnavailableError:
                return "typed"
            except RuntimeError:
                return "runtime"
            return "not-raised"

        assert asyncio.run(run()) == "runtime"
    finally:
        import app.providers.llama.streaming as streaming
        streaming.get_async_client = original


# ---------------------------------------------------------------------------
# SERVER: chat_relay emits the recoverable frame
# ---------------------------------------------------------------------------

class _RecordingSink:
    def __init__(self):
        self.frames = []
        self.closed = False

    @property
    def is_closed(self):
        return self.closed

    async def send_text(self, text):
        self.frames.append(json.loads(text))


class _DownProvider:
    async def chat_stream(self, **kwargs):
        from app.providers.llama.errors import BackendUnavailableError
        raise BackendUnavailableError(
            "engine exited before it was ready", model="m", oom=True
        )


def test_chat_relay_emits_recoverable_frame():
    from app.providers.coolems import chat_relay

    async def run():
        sink = _RecordingSink()
        ok = await chat_relay.run_chat_core(
            None, _DownProvider(), sink, model="m",
            messages=[{"role": "user", "content": "hi"}], conv_id="c",
        )
        assert ok is False
        errors = [f for f in sink.frames if f.get("type") == "error"]
        assert errors, sink.frames
        frame = errors[0]
        assert frame["error_type"] == "backend_unavailable"
        assert frame["recoverable"] is True
        assert frame["model"] == "m"
        assert frame["oom"] is True
        return "ok"

    assert asyncio.run(run()) == "ok"


class _ConnDownProvider:
    async def chat_stream(self, **kwargs):
        raise ConnectionError("llama.cpp service is not running. Please start llama-server.")


def test_chat_relay_tags_connection_error_as_recoverable():
    """A plain ConnectionError (Strata stopped / mid-stream drop) is ALSO recoverable.

    Before this fix only the typed BackendUnavailableError (503/OOM) was tagged, so a
    stopped Strata surfaced as a bare untagged error -> the client showed "Provider Error"
    with no resume dialog. A ConnectionError must be tagged backend_unavailable too.
    """
    from app.providers.coolems import chat_relay

    async def run():
        sink = _RecordingSink()
        ok = await chat_relay.run_chat_core(
            None, _ConnDownProvider(), sink, model="m",
            messages=[{"role": "user", "content": "hi"}], conv_id="c",
        )
        assert ok is False
        errors = [f for f in sink.frames if f.get("type") == "error"]
        assert errors, sink.frames
        frame = errors[0]
        assert frame["error_type"] == "backend_unavailable"
        assert frame["recoverable"] is True
        assert frame["model"] == "m"
        return "ok"

    assert asyncio.run(run()) == "ok"


# ---------------------------------------------------------------------------
# CLIENT: typed-error helper + resume/switch flow (subprocess isolation)
# ---------------------------------------------------------------------------

def test_client_raise_for_error_frame():
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{_CLIENT}')\n"
        "from app.providers.coolems_client.errors import raise_for_error_frame, BackendUnavailableError\n"
        "try:\n"
        "    raise_for_error_frame({'type':'error','message':'down','error_type':'backend_unavailable','model':'m','oom':True})\n"
        "except BackendUnavailableError as e:\n"
        "    assert e.model == 'm' and e.oom is True\n"
        "else:\n"
        "    raise SystemExit('typed error not raised')\n"
        "try:\n"
        "    raise_for_error_frame({'type':'error','message':'bad request'})\n"
        "except RuntimeError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('RuntimeError not raised')\n"
        "# Queue-worker watchdog frame: retryable + retry_after_sec (NOT error_type) must also be recoverable\n"
        "try:\n"
        "    raise_for_error_frame({'type':'error','message':'No backend is serving','retryable':True,'retry_after_sec':60,'model':'m'})\n"
        "except BackendUnavailableError as e:\n"
        "    assert e.model == 'm' and e.retry_after_sec == 60 and e.server_down is False\n"
        "else:\n"
        "    raise SystemExit('retryable frame not raised as recoverable')\n"
        "print('CLIENT_OK')\n"
    )
    r = _client_run(code)
    assert r.returncode == 0, r.stderr[-1500:]
    assert "CLIENT_OK" in r.stdout


def test_client_server_unreachable_error():
    """ServerUnreachableError is a recoverable backend-unavailable error with server_down.

    The client transport raises it when the SERVER itself (not the model backend) is down,
    so the resume loop knows to offer only wait/cancel and re-send when the SERVER returns.
    """
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{_CLIENT}')\n"
        "from app.providers.coolems_client.errors import ServerUnreachableError, BackendUnavailableError\n"
        "e = ServerUnreachableError('down', model='m')\n"
        "assert isinstance(e, BackendUnavailableError)\n"
        "assert isinstance(e, ConnectionError)\n"
        "assert e.server_down is True\n"
        "assert e.model == 'm'\n"
        "print('CLIENT_OK')\n"
    )
    r = _client_run(code)
    assert r.returncode == 0, r.stderr[-1500:]
    assert "CLIENT_OK" in r.stdout


def test_client_direct_mode_preserves_backend_unavailable():
    """The direct-WS transport must NOT flatten BackendUnavailableError.

    BackendUnavailableError subclasses ConnectionError -> OSError, so direct_mode's
    generic `except (ConnectionRefusedError, OSError)` used to swallow it into a plain
    "SERVER is not reachable" ConnectionError before it ever reached the resume loop.
    This pins the explicit re-raise (regression test for the 2026-11 audit fix).
    """
    code = (
        "import sys, asyncio, json, types\n"
        f"sys.path.insert(0, r'{_CLIENT}')\n"
        "_ts = types.ModuleType('app.providers.token_stats')\n"
        "class TokenStats:\n"
        "    def __init__(self, **kw): pass\n"
        "_ts.TokenStats = TokenStats\n"
        "sys.modules['app.providers.token_stats'] = _ts\n"
        "import websockets.exceptions  # resolve websockets.exceptions.ConnectionClosed\n"
        "\n"
        "from contextlib import asynccontextmanager\n"
        "from app.providers.coolems_client import direct_mode as dm\n"
        "from app.providers.coolems_client.errors import BackendUnavailableError\n"
        "\n"
        "class FakeWS:\n"
        "    def __init__(self):\n"
        "        self.sent = []\n"
        "        self._frames = [\n"
        "            {'type': 'auth_ok', 'protocol_version': 1, 'context_window': 262144},\n"
        "            {'type': 'error', 'error_type': 'backend_unavailable', 'message': 'down',\n"
        "             'model': 'm', 'oom': True, 'recoverable': True},\n"
        "        ]\n"
        "    async def send(self, text):\n"
        "        self.sent.append(json.loads(text))\n"
        "    async def recv(self):\n"
        "        if self._frames:\n"
        "            return json.dumps(self._frames.pop(0))\n"
        "        raise AssertionError('no more frames expected')\n"
        "    async def close(self):\n"
        "        pass\n"
        "\n"
        "@asynccontextmanager\n"
        "async def fake_connect(candidates, **kwargs):\n"
        "    yield FakeWS()\n"
        "\n"
        "class FakeProvider:\n"
        "    max_msg_size = 16 * 1024 * 1024\n"
        "    heartbeat_interval = 30\n"
        "    ping_timeout = 20\n"
        "    handshake_timeout = 15\n"
        "    _timeout = 30\n"
        "    def _get_ws_candidates(self):\n"
        "        return [('wss://x', None)]\n"
        "    def _load_api_key(self):\n"
        "        return 'key'\n"
        "\n"
        "dm.connect_with_failover = fake_connect\n"
        "dm._check_protocol = lambda *a, **k: None\n"
        "dm.apply_auth_info = lambda *a, **k: None\n"
        "\n"
        "class NullWS:\n"
        "    async def send_text(self, text): pass\n"
        "\n"
        "async def main():\n"
        "    try:\n"
        "        await dm.chat_stream_direct_websocket(\n"
        "            FakeProvider(), 'm', [{'role': 'user', 'content': 'hi'}], NullWS())\n"
        "    except BackendUnavailableError:\n"
        "        return 'typed'\n"
        "    except ConnectionError as e:\n"
        "        return 'flattened:' + str(e)\n"
        "    return 'not-raised'\n"
        "\n"
        "print(asyncio.run(main()))\n"
    )
    r = _client_run(code)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "typed" in r.stdout


def test_client_backend_resume_flow():
    code = (
        "import sys, asyncio, json\n"
        f"sys.path.insert(0, r'{_CLIENT}')\n"
        "from logic import backend_resume as br\n"
        "from app.providers.coolems_client.errors import BackendUnavailableError\n"
        "\n"
        "class FakeProvider:\n"
        "    def __init__(self):\n"
        "        self.switch_calls = []\n"
        "        self.chat_calls = []\n"
        "        self.models = [{'name':'failed-model','served':True,'online_slots':1}]\n"
        "        self.switch_result = (True, 'routed automatically')\n"
        "    async def get_models_list(self):\n"
        "        return self.models\n"
        "    async def switch_model(self, name):\n"
        "        self.switch_calls.append(name)\n"
        "        return self.switch_result\n"
        "    async def chat_stream(self, model, messages, ws, **kw):\n"
        "        self.chat_calls.append((model, messages))\n"
        "        return ('ok', None)\n"
        "\n"
        "class FakeWS:\n"
        "    def __init__(self): self.sent = []\n"
        "    async def send_text(self, t): self.sent.append(json.loads(t))\n"
        "\n"
        "class Cfg:\n"
        "    pass\n"
        "\n"
        "def make_cfg(provider):\n"
        "    c = Cfg()\n"
        "    c.provider = provider\n"
        "    c.websocket = FakeWS()\n"
        "    c.model = 'failed-model'\n"
        "    c.conversation_id = 'c1'\n"
        "    c.temperature = 0.7\n"
        "    c.enable_thinking = False\n"
        "    c.stop_event = None\n"
        "    c.ollama_tools = [{'function': {'name': 't1'}}]\n"
        "    return c\n"
        "\n"
        "async def main():\n"
        "    msgs = [{'role': 'user', 'content': 'hi'}]\n"
        "    # pure helpers\n"
        "    assert br._parse_switch_answer(\"Switch to 'alt-model'\", 'failed-model', ['alt-model']) == 'switch:alt-model'\n"
        "    assert br._parse_switch_answer('keep waiting', 'failed-model', []) == 'wait'\n"
        "    assert br._parse_switch_answer('cancel', 'failed-model', []) == 'cancel'\n"
        "    assert br._entry_matches({'name': 'alt-model.gguf'}, 'alt-model') is True\n"
        "    assert br._served_alternatives(\n"
        "        [{'name': 'failed-model', 'served': True},\n"
        "         {'name': 'alt-model', 'served': True},\n"
        "         {'name': 'offline-model', 'served': False}], 'failed-model') == ['alt-model']\n"
        "    # _switch_and_retry happy path: routing verified + re-targeted\n"
        "    p = FakeProvider()\n"
        "    c = make_cfg(p)\n"
        "    res, stats = await br._switch_and_retry(c, msgs, 'alt-model', BackendUnavailableError('x'))\n"
        "    assert res == 'ok' and c.model == 'alt-model'\n"
        "    assert p.switch_calls == ['alt-model']\n"
        "    assert p.chat_calls == [('alt-model', msgs)]\n"
        "    # _switch_and_retry unserved alternative fails cleanly, no re-send\n"
        "    p2 = FakeProvider(); p2.switch_result = (False, 'no node serving it')\n"
        "    c2 = make_cfg(p2)\n"
        "    try:\n"
        "        await br._switch_and_retry(c2, msgs, 'alt-model', BackendUnavailableError('x'))\n"
        "    except RuntimeError as e:\n"
        "        assert 'not served' in str(e)\n"
        "    else:\n"
        "        raise SystemExit('unserved alternative did not raise')\n"
        "    assert p2.chat_calls == []\n"
        "    # resume_after_backend_outage preserves the SAME messages\n"
        "    p3 = FakeProvider()\n"
        "    c3 = make_cfg(p3)\n"
        "    res3, stats3 = await br.resume_after_backend_outage(c3, msgs, BackendUnavailableError('x'))\n"
        "    assert res3 == 'ok'\n"
        "    assert p3.chat_calls and p3.chat_calls[0][1] is msgs\n"
        "    assert p3.chat_calls[0][0] == 'failed-model'\n"
        "    print('CLIENT_OK')\n"
        "\n"
        "asyncio.run(main())\n"
    )
    r = _client_run(code)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "CLIENT_OK" in r.stdout


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} passed")
    print("all strata-resilience checks passed")
