"""Tests: conversation ownership must be enforced on every per-conversation route.

conversations.ip_address is the workspace owner, stamped at creation. GET /conversations
filters on it, and every per-conversation route must enforce it too - otherwise any
authenticated caller could read, rename or delete another machine's workspace in the
shared chat_history.db. helpers.get_client_ip() must also not let a client pick its own
identity through a header.

Two contracts are pinned here:
  1. get_client_ip() uses the socket address; X-Forwarded-For only when
     config.TRUST_PROXY_HEADERS is True.
  2. _require_conversation_owner() -> 404 when missing, 403 for another owner, allowed for
     the owner and for rows with no recorded owner ('unknown'/NULL: the schema default and
     the WebSocket fallback in app/websocket/db_ops).

The CLIENT modules are imported inside a CHILD interpreter with cwd=CLIENT/ - exactly how
code_client.py runs them. In-process imports would collide with the SERVER root's own
'app'/'config' packages, which pytest puts on sys.path first.

Run:  venv/Scripts/python.exe -m pytest tools/test_tools/test_conversation_ownership.py -v
"""

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_CLIENT_ROOT = os.path.join(_REPO_ROOT, "CLIENT")
_CONVERSATIONS_FILE = os.path.join(_CLIENT_ROOT, "app", "routers", "conversations.py")

# Every route that addresses ONE conversation (path contains {conv_id}).
OWNED_ROUTES = (
    "get_messages",
    "get_conversation_status",
    "update_conversation",
    "delete_conversation",
    "truncate_messages",
    "reset_conversation_state",
)

_CHILD = r"""
import json, os, sqlite3, sys, tempfile, types

sys.path.insert(0, os.getcwd())          # cwd = CLIENT root, like code_client.py
out = {}

try:
    from fastapi import HTTPException
    import app.helpers as helpers
    from app.routers.conversations import NO_OWNER, _require_conversation_owner

    def req(headers=None, host="10.0.0.5"):
        return types.SimpleNamespace(headers=headers or {},
                                     client=types.SimpleNamespace(host=host))

    def status_of(call):
        try:
            call()
        except HTTPException as exc:
            return exc.status_code
        return None

    tmp = tempfile.mkdtemp()
    db = sqlite3.connect(os.path.join(tmp, "own.db"))
    db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, ip_address TEXT)")
    db.execute("INSERT INTO conversations VALUES ('c1', 't', '10.0.0.5')")
    db.execute("INSERT INTO conversations VALUES ('c2', 't', ?)", (NO_OWNER,))
    db.execute("INSERT INTO conversations VALUES ('c3', 't', NULL)")
    db.commit()

    # 1. identity source
    out["header_ignored"] = helpers.get_client_ip(req({"X-Forwarded-For": "1.2.3.4"})) == "10.0.0.5"
    out["socket_default"] = helpers.get_client_ip(req(host="127.0.0.1")) == "127.0.0.1"
    helpers.TRUST_PROXY_HEADERS = True
    out["header_with_flag"] = (
        helpers.get_client_ip(req({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})) == "1.2.3.4"
    )
    helpers.TRUST_PROXY_HEADERS = False

    # 2. ownership guard
    out["owner_allowed"] = _require_conversation_owner(db.cursor(), "c1", "10.0.0.5") == "10.0.0.5"
    out["foreign_403"] = status_of(
        lambda: _require_conversation_owner(db.cursor(), "c1", "10.9.9.9")) == 403
    out["missing_404"] = status_of(
        lambda: _require_conversation_owner(db.cursor(), "nope", "10.0.0.5")) == 404
    out["unknown_owner_allowed"] = (
        _require_conversation_owner(db.cursor(), "c2", "10.9.9.9") == NO_OWNER)
    out["null_owner_allowed"] = _require_conversation_owner(db.cursor(), "c3", "10.9.9.9") is None
    db.close()
except Exception as e:
    out["import_error"] = f"{type(e).__name__}: {e}"

print("RESULT:" + json.dumps(out))
"""


def _child_results():
    """Run the CLIENT checks once in an isolated interpreter and return their dict."""
    res = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=_CLIENT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        },
    )
    if res.returncode != 0:
        raise AssertionError(
            f"CLIENT check process exited {res.returncode}: {res.stderr[-1500:]}"
        )
    for line in res.stdout.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    raise AssertionError(f"no RESULT line from CLIENT check: {res.stdout[:1500]}")


_RESULTS = _child_results()


def _assert_check(key):
    assert "import_error" not in _RESULTS, _RESULTS["import_error"]
    assert _RESULTS.get(key) is True, f"{key} failed: {_RESULTS}"


def test_forwarded_header_ignored_by_default():
    """No reverse proxy -> the socket address is the identity, the header cannot lie."""
    _assert_check("header_ignored")


def test_socket_address_is_the_default_identity():
    _assert_check("socket_default")


def test_forwarded_header_honoured_only_when_proxy_trusted():
    """Behind an operator-controlled proxy the socket is the proxy, so the header is used."""
    _assert_check("header_with_flag")


def test_guard_allows_the_owner():
    _assert_check("owner_allowed")


def test_guard_blocks_other_owner():
    _assert_check("foreign_403")


def test_guard_missing_conversation_is_404():
    _assert_check("missing_404")


def test_guard_keeps_ownerless_rows_reachable():
    """'unknown'/NULL rows come from the schema default and the WebSocket fallback."""
    _assert_check("unknown_owner_allowed")
    _assert_check("null_owner_allowed")


def test_every_per_conversation_route_uses_the_guard():
    """Structural pin: a route that forgets the guard must fail here."""
    src = open(_CONVERSATIONS_FILE, encoding="utf-8").read()
    missing = [name for name in OWNED_ROUTES if f"async def {name}(" not in src]
    assert not missing, f"route(s) gone from conversations.py: {missing}"

    for name in OWNED_ROUTES:
        body = src.split(f"async def {name}(", 1)[1].split("@router.", 1)[0]
        assert "_require_conversation_owner(" in body, f"{name} does not enforce ownership"


if __name__ == "__main__":
    for fn in (
        test_forwarded_header_ignored_by_default,
        test_socket_address_is_the_default_identity,
        test_forwarded_header_honoured_only_when_proxy_trusted,
        test_guard_allows_the_owner,
        test_guard_blocks_other_owner,
        test_guard_missing_conversation_is_404,
        test_guard_keeps_ownerless_rows_reachable,
        test_every_per_conversation_route_uses_the_guard,
    ):
        fn()
    print("conversation ownership checks passed")
