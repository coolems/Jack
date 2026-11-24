"""Ownership guard + no-fallback contract for workspace file access (regression coverage).

The files router resolves a workspace's FOLDER from a caller-supplied conversation id.
These tests pin the rules that make that safe:

  * no workspace scope claimed            -> 400 (never "use the process-global folder")
  * not a workspace id / no such row      -> 404 (never "use another folder")
  * owned by another client               -> 403
  * stored folder no longer exists        -> 409 (never "use the project root")

...and every one of those refusals is reported through app.security_events, so a rogue
client call cannot disappear into a 4xx response.
"""
import asyncio
import os
import sqlite3
import sys
from types import ModuleType

import pytest
from fastapi import HTTPException

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Autouse fixture: CLIENT modules are imported as `app` (see _client_imports for why).
from tools.test_tools._client_imports import client_app_scope, load_module

OWN_IP = "10.0.0.7"
OTHER_IP = "10.0.0.99"
OWN_WS = "ownwsaa"




def make_db(rows):
    """In-memory conversations table; rows are (id, ip_address, working_root)."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, ip_address TEXT, working_root TEXT)")
    db.executemany("INSERT INTO conversations VALUES (?, ?, ?)", rows)
    db.commit()
    return db


@pytest.fixture
def events(monkeypatch):
    """Real security_events module with the JSONL write stubbed out (no test pollution)."""
    from app import security_events

    security_events.clear()
    monkeypatch.setattr(security_events, "_write", lambda event: None)
    yield security_events
    security_events.clear()


@pytest.fixture
def access():
    return load_module("CLIENT/app/workspace_access.py", "_workspace_access_test")


def kinds(events):
    return [event["kind"] for event in events.recent(50)]


def test_unknown_workspace_is_rejected_not_redirected(access, events):
    db = make_db([(OWN_WS, OWN_IP, None)])
    with pytest.raises(HTTPException) as err:
        access.require_workspace_access(db.cursor(), "nosuchw", OWN_IP)
    assert err.value.status_code == 404
    assert "workspace_unknown" in kinds(events)
    db.close()


def test_blank_or_non_string_id_is_rejected(access, events):
    db = make_db([(OWN_WS, OWN_IP, None)])
    for bad in ("", "   ", None, 42):
        with pytest.raises(HTTPException) as err:
            access.require_workspace_access(db.cursor(), bad, OWN_IP)
        assert err.value.status_code == 404
    # All four are counted; the two blank claims coalesce into one written event.
    assert events.summary()["by_kind"]["workspace_id_invalid"] == 4
    assert kinds(events).count("workspace_id_invalid") == 3
    db.close()


def test_malformed_id_never_reaches_the_query(access, events):
    """'../../etc' or a 10 KB id must be refused before it becomes a SQL parameter."""
    db = make_db([(OWN_WS, OWN_IP, None)])
    for bad in ("../../etc", "x" * 5000, "OWNWSA1"):
        with pytest.raises(HTTPException) as err:
            access.require_workspace_access(db.cursor(), bad, OWN_IP)
        assert err.value.status_code == 404
    assert kinds(events).count("workspace_id_invalid") == 3
    db.close()


def test_foreign_workspace_is_forbidden_and_reported(access, events):
    db = make_db([(OWN_WS, OTHER_IP, "/tmp/secret")])
    with pytest.raises(HTTPException) as err:
        access.require_workspace_access(db.cursor(), OWN_WS, OWN_IP)
    assert err.value.status_code == 403
    reported = [event for event in events.recent(5) if event["kind"] == "workspace_foreign"]
    assert reported and reported[0]["owner_ip"] == OTHER_IP and reported[0]["client_ip"] == OWN_IP
    db.close()


def test_own_workspace_returns_owner_and_stored_root(access, events):
    db = make_db([(OWN_WS, OWN_IP, "/tmp/mine")])
    owner_ip, stored_root = access.require_workspace_access(db.cursor(), OWN_WS, OWN_IP)
    assert owner_ip == OWN_IP
    assert stored_root == "/tmp/mine"
    assert kinds(events) == []  # a clean, owned request is not an event
    db.close()


def test_unowned_legacy_rows_stay_reachable_but_are_reported(access, events):
    # 'unknown' is the schema default and what the chat socket writes; NULL is a legacy row.
    # Neither has an identity to compare, so both stay reachable - never silently.
    db = make_db([("unownaa", "unknown", "/tmp/a"), ("unownbb", None, "/tmp/b")])
    assert access.require_workspace_access(db.cursor(), "unownaa", OWN_IP)[1] == "/tmp/a"
    assert access.require_workspace_access(db.cursor(), "unownbb", OWN_IP)[1] == "/tmp/b"
    assert kinds(events) == ["workspace_unowned", "workspace_unowned"]
    db.close()


def test_authorize_workspace_opens_and_closes_its_own_connection(access, events, monkeypatch):
    db = make_db([(OWN_WS, OWN_IP, "/tmp/mine")])
    opened = {"n": 0, "closed": 0}

    manager = ModuleType("app.db_manager")

    def get_db_connection():
        opened["n"] += 1
        return db

    manager.get_db_connection = get_db_connection
    manager.close_db_connection = lambda conn: opened.__setitem__("closed", opened["closed"] + 1)
    monkeypatch.setitem(sys.modules, manager.__name__, manager)

    assert access.authorize_workspace(OWN_WS, OWN_IP) == "/tmp/mine"
    assert opened == {"n": 1, "closed": 1}
    with pytest.raises(HTTPException) as err:
        access.authorize_workspace("nosuchw", OWN_IP)
    assert err.value.status_code == 404
    assert opened["closed"] == 2  # still closed on the failing path


class _FakeHeaders(dict):
    def __init__(self, headers=None):
        super().__init__()
        for key, value in (headers or {}).items():
            self[str(key).lower()] = value

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class _FakeRequest:
    method = "GET"

    def __init__(self, query_params=None, headers=None):
        self.query_params = query_params or {}
        self.headers = _FakeHeaders(headers)
        self.url = type("URL", (), {"path": "/api/tree"})()


def _wire(middleware_name, monkeypatch, rows, tmp_path, published, folder_missing=False):
    """Load the files middleware with the real guard and a stub DB / client-ip helper."""
    access = load_module("CLIENT/app/workspace_access.py", "_workspace_access_dep")
    middleware = load_module("CLIENT/app/routers/files/middleware.py", middleware_name)
    db = make_db(rows)
    opens = {"n": 0}

    manager = ModuleType("app.db_manager")

    def get_db_connection():
        opens["n"] += 1
        return db

    manager.get_db_connection = get_db_connection
    manager.close_db_connection = lambda conn: None
    monkeypatch.setitem(sys.modules, manager.__name__, manager)

    helpers = ModuleType("app.helpers")
    helpers.get_client_ip = lambda request: OWN_IP
    monkeypatch.setitem(sys.modules, helpers.__name__, helpers)

    common = ModuleType("app.utils.common")

    class WorkspaceFolderMissing(RuntimeError):
        def __init__(self, path):
            super().__init__(path)
            self.path = path

        def __str__(self):
            return str(self.path)

    def resolve(stored, conv_id=None):
        if folder_missing:
            raise WorkspaceFolderMissing(str(tmp_path / "gone"))
        return str(tmp_path / published)

    common.WorkspaceFolderMissing = WorkspaceFolderMissing
    common.resolve_working_root_from_row = resolve
    monkeypatch.setitem(sys.modules, common.__name__, common)

    monkeypatch.setitem(sys.modules, "app.workspace_access", access)
    return middleware, db, opens


def test_dependency_refuses_foreign_workspace_without_publishing_a_folder(tmp_path, monkeypatch, events):
    middleware, db, _ = _wire("_files_middleware_foreign", monkeypatch, [(OWN_WS, OTHER_IP, None)], tmp_path, "ws")
    request = _FakeRequest(query_params={"conversation_id": OWN_WS})
    middleware._file_request_working_root.set(str(tmp_path / "previous"))

    with pytest.raises(HTTPException) as err:
        asyncio.run(middleware.resolve_file_working_root(request))
    assert err.value.status_code == 403
    # The previous request's folder must not survive into a refused request.
    assert middleware._file_request_working_root.get() == str(tmp_path / "previous")
    assert "workspace_foreign" in kinds(events)
    db.close()


def test_dependency_refuses_unknown_workspace(tmp_path, monkeypatch, events):
    middleware, db, _ = _wire("_files_middleware_unknown", monkeypatch, [(OWN_WS, OWN_IP, None)], tmp_path, "ws")
    request = _FakeRequest(headers={"X-Conversation-Id": "nosuchw"})

    with pytest.raises(HTTPException) as err:
        asyncio.run(middleware.resolve_file_working_root(request))
    assert err.value.status_code == 404
    assert "workspace_unknown" in kinds(events)
    db.close()


def test_dependency_requires_a_workspace_scope(tmp_path, monkeypatch, events):
    """No id is NOT a licence to use the process-global folder - it is a 400."""
    middleware, db, opens = _wire("_files_middleware_noid", monkeypatch, [], tmp_path, "ws")

    with pytest.raises(HTTPException) as err:
        asyncio.run(middleware.resolve_file_working_root(_FakeRequest()))
    assert err.value.status_code == 400
    assert opens["n"] == 0  # nothing claimed -> nothing looked up
    assert middleware._file_request_working_root.get() is None
    reported = [event for event in events.recent(5) if event["kind"] == "workspace_scope_missing"]
    assert reported and reported[0]["path"] == "/api/tree" and reported[0]["method"] == "GET"
    db.close()


def test_dependency_refuses_a_vanished_workspace_folder(tmp_path, monkeypatch, events):
    """A stored folder that no longer exists must 409, never slide to the project root."""
    middleware, db, _ = _wire(
        "_files_middleware_missing", monkeypatch, [(OWN_WS, OWN_IP, "gone")], tmp_path, "ws", folder_missing=True
    )

    with pytest.raises(HTTPException) as err:
        asyncio.run(middleware.resolve_file_working_root(_FakeRequest(query_params={"conversation_id": OWN_WS})))
    assert err.value.status_code == 409
    assert middleware._file_request_working_root.get() is None
    assert "workspace_folder_missing" in kinds(events)
    db.close()


def _call_dependency(middleware, request):
    """Run the dependency and report what it published.

    The read must happen INSIDE the coroutine: asyncio.run() executes in a copy of the
    caller's context, exactly like Starlette's request task does (the endpoint then sees
    what its async dependency stored).
    """
    async def run():
        await middleware.resolve_file_working_root(request)
        return middleware._file_request_working_root.get(), middleware.request_workspace_id()

    return asyncio.run(run())


def test_dependency_publishes_own_workspace_folder_from_one_query(tmp_path, monkeypatch, events):
    middleware, db, opens = _wire("_files_middleware_own", monkeypatch, [(OWN_WS, OWN_IP, "stored")], tmp_path, "ws")
    request = _FakeRequest(query_params={"conversation_id": OWN_WS})

    published, scoped = _call_dependency(middleware, request)
    assert published == str(tmp_path / "ws")
    assert scoped == OWN_WS  # POST /api/working_root binds to this, never to a body id
    assert opens["n"] == 1
    db.close()


def test_get_working_root_dir_has_no_fallback(tmp_path, monkeypatch, events):
    """An endpoint reached without the dependency must fail loudly instead of guessing."""
    middleware = load_module("CLIENT/app/routers/files/middleware.py", "_files_middleware_unscoped")
    middleware._file_request_working_root.set(None)

    with pytest.raises(RuntimeError, match="not resolved"):
        middleware._get_working_root_dir()
    assert "working_root_unscoped" in kinds(events)


def test_path_escape_is_refused_and_reported(tmp_path, events):
    middleware = load_module("CLIENT/app/routers/files/middleware.py", "_files_middleware_escape")
    base = tmp_path / "ws"
    base.mkdir()

    assert middleware._safe_relative_path(str(base), "../outside.txt") is None
    reported = [event for event in events.recent(5) if event["kind"] == "path_escape"]
    assert reported and reported[0]["requested"] == "../outside.txt"
    assert middleware._safe_relative_path(str(base), "inside.txt") == str(base / "inside.txt")
