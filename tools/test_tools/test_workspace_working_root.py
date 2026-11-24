"""Regression coverage for workspace folder selection in tools and prompts."""
import asyncio
import os
import sqlite3
import sys
from types import ModuleType

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Autouse fixture: CLIENT modules are imported as `app` (see _client_imports for why).
from tools.test_tools._client_imports import client_app_scope, load_module


def load_source(path, module_name="_working_root_test"):
    return load_module(path, module_name)


def test_replace_legacy_server_root_in_first_prompt():
    prompts = load_source("CLIENT/app/prompts/__init__.py")
    server_root = r"C:\Transfer\GitHub\Jack"
    workspace_root = r"C:\Transfer\GitHub\MARK"
    legacy_prompt = (
        "Your name is Jack.\n"
        f"Your working_root (default file operations folder) is: {server_root}\n\n"
        "## IMPORTANT - YOUR ROOT FOLDER\n"
        "- Your root folder is dynamically set in your identity (see above).\n"
        "- ALWAYS use the work_folder from your identity.\n\n"
        "## RESPONSE RULES\n- Preserve these rules.\n"
    )
    rendered = prompts.with_workspace_working_root(legacy_prompt, workspace_root)
    assert rendered.count(workspace_root) == 1
    assert server_root not in rendered
    assert "work_folder from your identity" not in rendered
    assert "Preserve these rules" in rendered
    assert "use an empty directory argument" in rendered
    assert workspace_root in prompts.with_workspace_working_root("Your name is Jack.", workspace_root)


def test_concurrent_tool_calls_use_their_workspace_root(tmp_path):
    utils = load_source("tools/utils.py")
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    default = utils.get_working_root()

    async def run():
        ready = asyncio.Event()
        opened = 0

        async def tool_turn(path):
            nonlocal opened
            try:
                utils.set_current_working_root(path)
                opened += 1
                if opened == 2:
                    ready.set()
                await ready.wait()
                return await asyncio.to_thread(utils.get_working_root)
            finally:
                utils.set_current_working_root(None)

        a = asyncio.create_task(tool_turn(first))
        await asyncio.sleep(0)
        assert utils.get_working_root() == default
        b = asyncio.create_task(tool_turn(second))
        assert await asyncio.gather(a, b) == [str(first), str(second)]
        assert utils.get_working_root() == default

    asyncio.run(run())


def test_switching_reads_latest_database_setting_without_writing(tmp_path, monkeypatch):
    db = sqlite3.connect(":memory:")
    with db:
        db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, working_root TEXT)")
        db.executemany("INSERT INTO conversations (id) VALUES (?)", [("first",), ("second",)])
    manager = ModuleType("app.db_manager")
    manager.get_db_connection = lambda: db
    manager.close_db_connection = lambda conn: None
    monkeypatch.setitem(sys.modules, manager.__name__, manager)
    try:
        roots = load_source("CLIENT/app/utils/common/working_root.py")
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        roots.set_working_root(str(first), "first")
        roots.set_working_root(str(second), "second")
        assert roots.resolve_working_root_for_conv("first") == str(first)
        assert roots.get_working_root() == str(second)
        writes = db.total_changes
        assert roots.set_working_root("", "first") == str(first)
        assert db.total_changes == writes
        monkeypatch.setattr(roots, "_write_conversation_working_root", lambda *_: False)
        with pytest.raises(RuntimeError, match="Could not save"):
            roots.set_working_root(str(second), "first")
        assert roots.get_working_root() == str(first)
    finally:
        db.close()


class _FakeHeaders(dict):
    def __init__(self, *args, **kwargs):
        super().__init__()
        if args and isinstance(args[0], dict):
            for k, v in args[0].items():
                self[str(k).lower()] = v
        for k, v in kwargs.items():
            self[str(k).lower()] = v

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class _FakeRequest:
    def __init__(self, query_params=None, headers=None):
        self.query_params = query_params or {}
        self.headers = _FakeHeaders(headers or {})


def test_file_endpoint_working_root_comes_only_from_the_request_contextvar(tmp_path, monkeypatch):
    """There is no second source: without a request-scoped folder the endpoint refuses."""
    from app import security_events

    monkeypatch.setattr(security_events, "_write", lambda event: None)
    middleware = load_source("CLIENT/app/routers/files/middleware.py")
    target = tmp_path / "ws"
    target.mkdir()
    fallback = tmp_path / "global"
    fallback.mkdir()

    common = ModuleType("app.utils.common")
    calls = {"n": 0}

    def fake_get_working_root():
        calls["n"] += 1
        return str(fallback)

    common.get_working_root = fake_get_working_root
    monkeypatch.setitem(sys.modules, common.__name__, common)

    middleware._file_request_working_root.set(str(target))
    try:
        assert middleware._get_working_root_dir() == str(target)
    finally:
        middleware._file_request_working_root.set(None)

    # The process-global working root is whatever workspace was activated last by ANY
    # client, so it must never be consulted for a file request.
    with pytest.raises(RuntimeError):
        middleware._get_working_root_dir()
    assert calls["n"] == 0


def test_resolve_file_working_root_reads_query_then_header(tmp_path, monkeypatch):
    """The dependency takes the id from the query first, then the header, and refuses a
    request that claims no workspace at all (ownership coverage:
    test_workspace_access_guard.py)."""
    from app import security_events
    from fastapi import HTTPException

    monkeypatch.setattr(security_events, "_write", lambda event: None)
    middleware = load_source("CLIENT/app/routers/files/middleware.py")

    access = load_source("CLIENT/app/workspace_access.py", "app.workspace_access")
    monkeypatch.setitem(sys.modules, "app.workspace_access", access)

    seen = []

    class _Cursor:
        def execute(self, sql, params):
            seen.append(params)

        def fetchone(self):
            return ("1.2.3.4", str(tmp_path / "ws"))

    class _Conn:
        def cursor(self):
            return _Cursor()

    manager = ModuleType("app.db_manager")
    manager.get_db_connection = _Conn
    manager.close_db_connection = lambda conn: None
    monkeypatch.setitem(sys.modules, manager.__name__, manager)

    helpers = ModuleType("app.helpers")
    helpers.get_client_ip = lambda request: "1.2.3.4"
    monkeypatch.setitem(sys.modules, helpers.__name__, helpers)

    common = ModuleType("app.utils.common")

    class WorkspaceFolderMissing(RuntimeError):
        pass

    common.WorkspaceFolderMissing = WorkspaceFolderMissing
    common.resolve_working_root_from_row = lambda stored, conv_id=None: str(tmp_path / conv_id)
    monkeypatch.setitem(sys.modules, common.__name__, common)

    async def run():
        req = _FakeRequest(
            query_params={"conversation_id": "aaabbbb"},
            headers={"x-conversation-id": "ccccccc"},
        )
        await middleware.resolve_file_working_root(req)
        assert middleware._file_request_working_root.get() == str(tmp_path / "aaabbbb")
        assert seen[-1] == ("aaabbbb",)          # query wins over the header

        req = _FakeRequest(query_params={}, headers={"X-Conversation-Id": "ddddddd"})
        await middleware.resolve_file_working_root(req)
        assert middleware._file_request_working_root.get() == str(tmp_path / "ddddddd")
        assert seen[-1] == ("ddddddd",)

        req = _FakeRequest()
        with pytest.raises(HTTPException) as err:
            await middleware.resolve_file_working_root(req)
        assert err.value.status_code == 400   # no scope -> no lookup, no folder
        assert len(seen) == 2

    asyncio.run(run())


def test_resolve_working_root_from_row_matches_the_stored_value(tmp_path, monkeypatch):
    manager = ModuleType("app.db_manager")
    manager.get_db_connection = lambda: None
    manager.close_db_connection = lambda conn: None
    monkeypatch.setitem(sys.modules, manager.__name__, manager)
    roots = load_source("CLIENT/app/utils/common/working_root.py")

    existing = tmp_path / "exists"
    existing.mkdir()
    assert roots.resolve_working_root_from_row(str(existing)) == str(existing)
    assert roots.resolve_working_root_from_row(None) == roots.get_working_root()
    # A stored folder that no longer exists must NOT become the project root: pointing
    # deletes, uploads and run-python at files nobody picked is the bug this guards.
    with pytest.raises(roots.WorkspaceFolderMissing):
        roots.resolve_working_root_from_row(str(tmp_path / "gone"))

