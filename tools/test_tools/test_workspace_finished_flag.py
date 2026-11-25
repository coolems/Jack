"""Regression coverage for the persistent "finished & unread" workspace flag (2026-11-24).

A workspace that finishes a task must glow in the sidebar until the user opens it, and that
state must survive a hard restart. The single source of truth is conversations.unread_finished:

  * database.py adds the column (fresh schema + legacy migration, default 0);
  * workspace_finished.py flips it, never raising (a failing write must not break a turn);
  * conversations.py returns it in the list and clears it only via POST /{id}/seen;
  * the UI side (conversation-manager.js / chat-socket-pool.js) drives the glow from it and
    drops the old 30 s "done" expiry so the marker lasts until the user clicks.
"""
import asyncio
import sqlite3
import sys
import types
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Autouse fixture: CLIENT modules are imported as `app` (see _client_imports for why).
from tools.test_tools._client_imports import client_app_scope, load_module  # noqa: F401

OWN_IP = "10.0.0.7"
OTHER_IP = "10.0.0.99"

CONV_SCHEMA = """
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    model TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ip_address TEXT,
    working_root TEXT,
    color TEXT,
    parent_id TEXT,
    unread_finished INTEGER DEFAULT 0
)
"""


@pytest.fixture
def quiet_security(monkeypatch):
    """Real security_events module with the JSONL write stubbed (no test pollution)."""
    from app import security_events

    security_events.clear()
    monkeypatch.setattr(security_events, "_write", lambda event: None)
    yield security_events
    security_events.clear()


def _load_conversations(monkeypatch, db):
    """Load conversations.py with db/helpers/turn_ops stubbed; keep the real ownership guard."""
    import app.workspace_tree  # real (under client_app_scope)
    import app.workspace_access  # real

    monkeypatch.setitem(sys.modules, "app.workspace_tree", app.workspace_tree)
    monkeypatch.setitem(sys.modules, "app.workspace_access", app.workspace_access)

    manager = ModuleType("app.db_manager")
    manager.get_db_connection = lambda: db
    manager.close_db_connection = lambda conn: None
    monkeypatch.setitem(sys.modules, "app.db_manager", manager)

    helpers = ModuleType("app.helpers")
    helpers.get_client_ip = lambda request: OWN_IP
    monkeypatch.setitem(sys.modules, "app.helpers", helpers)

    # Avoid importing the heavy real app.websocket package (its __init__ pulls the handler).
    ws_pkg = ModuleType("app.websocket")
    ws_pkg.__path__ = []
    turn_ops = ModuleType("app.websocket.turn_ops")
    turn_ops.delete_conversation_rows = lambda cursor, conv_id: {"turn_log": 0, "messages": 0, "conversations": 0}
    turn_ops.truncate_turn_logs = lambda *a, **k: 0
    turn_ops.load_turn_logs = lambda conv_id: []
    ws_pkg.turn_ops = turn_ops
    monkeypatch.setitem(sys.modules, "app.websocket", ws_pkg)
    monkeypatch.setitem(sys.modules, "app.websocket.turn_ops", turn_ops)

    conversations = load_module("CLIENT/app/routers/conversations.py", "_conversations_finished_test")
    agent = types.SimpleNamespace(get_name=lambda: "Jack")
    router = conversations.create_conversations_router(":memory:", "test-model", agent)
    return conversations, router


class _FakeRequest:
    pass


def _route(router, path_suffix, method=None):
    for r in router.routes:
        if not getattr(r, "path", "").endswith(path_suffix):
            continue
        if method is not None and method not in (getattr(r, "methods", None) or set()):
            continue
        return r
    raise AssertionError(f"route not found: {path_suffix} {method}")


# ===== migration =====


def test_migration_adds_unread_finished_to_legacy_db(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(db_path))
    legacy.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, ip_address TEXT)")
    legacy.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT, role TEXT, content TEXT)")
    legacy.execute("INSERT INTO conversations (id, title, ip_address) VALUES ('oldwsxx', 'Old', '1.2.3.4')")
    legacy.commit()
    legacy.close()

    manager = ModuleType("app.db_manager")
    manager.init_db_manager = lambda path: None
    manager.get_db_connection = lambda: sqlite3.connect(str(db_path))
    manager.close_db_connection = lambda conn: conn.close()
    monkeypatch.setitem(sys.modules, "app.db_manager", manager)

    tree = ModuleType("app.workspace_tree")
    tree.migrate_workspace_ids = lambda cursor: None
    monkeypatch.setitem(sys.modules, "app.workspace_tree", tree)

    cfg = ModuleType("client_config")
    cfg.MODEL_NAME = "test-model"
    monkeypatch.setitem(sys.modules, "client_config", cfg)

    database = load_module("CLIENT/app/database.py", "_database_finished_test")
    database.init_db(str(db_path))

    check = sqlite3.connect(str(db_path))
    try:
        cols = [r[1] for r in check.execute("PRAGMA table_info(conversations)").fetchall()]
        assert "unread_finished" in cols
        # Legacy rows default to 0: no surprise glow after upgrading.
        assert check.execute("SELECT unread_finished FROM conversations WHERE id='oldwsxx'").fetchone()[0] == 0
    finally:
        check.close()


# ===== workspace_finished helpers =====


def test_mark_and_clear_helpers_flip_flag(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, unread_finished INTEGER DEFAULT 0)")
    db.execute("INSERT INTO conversations (id) VALUES ('ownwsaa')")
    db.commit()

    manager = ModuleType("app.db_manager")
    manager.get_db_connection = lambda: db
    manager.close_db_connection = lambda conn: None
    monkeypatch.setitem(sys.modules, "app.db_manager", manager)

    wf = load_module("CLIENT/app/workspace_finished.py", "_workspace_finished_flag_test")
    wf.mark_workspace_finished("ownwsaa")
    assert db.execute("SELECT unread_finished FROM conversations WHERE id='ownwsaa'").fetchone()[0] == 1
    wf.clear_workspace_finished("ownwsaa")
    assert db.execute("SELECT unread_finished FROM conversations WHERE id='ownwsaa'").fetchone()[0] == 0
    # Blank id is a no-op, never raises, never touches the DB.
    wf.mark_workspace_finished("")
    wf.clear_workspace_finished("")
    db.close()


def test_mark_helpers_never_raise_on_db_error(monkeypatch):
    manager = ModuleType("app.db_manager")

    def boom():
        raise RuntimeError("db down")

    manager.get_db_connection = boom
    manager.close_db_connection = lambda conn: None
    monkeypatch.setitem(sys.modules, "app.db_manager", manager)

    wf = load_module("CLIENT/app/workspace_finished.py", "_workspace_finished_neverraise_test")
    assert wf.mark_workspace_finished("ownwsaa") is None
    assert wf.clear_workspace_finished("ownwsaa") is None


# ===== conversations list + /seen =====


def test_list_returns_unread_finished(monkeypatch, quiet_security):
    db = sqlite3.connect(":memory:")
    db.execute(CONV_SCHEMA)
    db.execute("INSERT INTO conversations (id, title, ip_address, unread_finished) VALUES ('ownwsaa', 'Mine', ?, 1)", (OWN_IP,))
    db.commit()

    _, router = _load_conversations(monkeypatch, db)
    route = _route(router, "/api/conversations", "GET")
    result = asyncio.run(route.endpoint(_FakeRequest(), sort_by="created", sort_order="desc"))

    assert result["conversations"][0]["id"] == "ownwsaa"
    assert result["conversations"][0]["unread_finished"] is True
    db.close()


def test_seen_clears_own_workspace(monkeypatch, quiet_security):
    db = sqlite3.connect(":memory:")
    db.execute(CONV_SCHEMA)
    db.execute("INSERT INTO conversations (id, title, ip_address, unread_finished) VALUES ('ownwsaa', 'Mine', ?, 1)", (OWN_IP,))
    db.commit()

    _, router = _load_conversations(monkeypatch, db)
    route = _route(router, "/seen", "POST")
    result = asyncio.run(route.endpoint("ownwsaa", _FakeRequest()))

    assert result == {"status": "seen"}
    assert db.execute("SELECT unread_finished FROM conversations WHERE id='ownwsaa'").fetchone()[0] == 0
    db.close()


def test_seen_guards_foreign_and_unknown(monkeypatch, quiet_security):
    db = sqlite3.connect(":memory:")
    db.execute(CONV_SCHEMA)
    db.execute("INSERT INTO conversations (id, title, ip_address, unread_finished) VALUES ('foreign', 'Other', ?, 1)", (OTHER_IP,))
    db.commit()

    _, router = _load_conversations(monkeypatch, db)
    route = _route(router, "/seen", "POST")

    with pytest.raises(HTTPException) as err:
        asyncio.run(route.endpoint("foreign", _FakeRequest()))
    assert err.value.status_code == 403

    with pytest.raises(HTTPException) as err:
        asyncio.run(route.endpoint("nosuchw", _FakeRequest()))
    assert err.value.status_code == 404

    # The foreign row's flag is untouched by a refused clear.
    assert db.execute("SELECT unread_finished FROM conversations WHERE id='foreign'").fetchone()[0] == 1
    db.close()


# ===== UI static contract =====


def test_static_contracts():
    cm = (_REPO_ROOT / "CLIENT/static/ui/conversation-manager.js").read_text(encoding="utf-8")
    assert "workspace-finished" in cm
    assert "markSeen" in cm
    # (2026-11-25 audit) a reload also renders the green done badge from the DB flag,
    # not just the glow outline - the persisted row matches the live background completion.
    assert "unread_finished ? 'done'" in cm

    sp = (_REPO_ROOT / "CLIENT/static/ui/chat-socket-pool.js").read_text(encoding="utf-8")
    assert "workspace-finished" in sp
    assert "30000" not in sp  # the 30 s 'done' expiry is gone -> marker lasts until the user clicks
    # (2026-11-25 root-cause fix) a COMPLETED 'cycle_end' must NOT flip the status dot to idle:
    # the backend emits it immediately BEFORE 'done' (history settles first), so treating it as
    # idle would starve the background 'done' attention guard (prev === 'generating'/'queued')
    # and a finished background workspace would never glow while the user is in another workspace.
    # Only a STOPPED cycle is terminal for the dot (stop/cancel never glows).
    assert "cycle_end') st = 'idle'" not in sp
    assert "msg.payload.final.stopped" in sp

    conv = (_REPO_ROOT / "CLIENT/app/routers/conversations.py").read_text(encoding="utf-8")
    assert "/{conv_id}/seen" in conv
    assert "unread_finished" in conv
