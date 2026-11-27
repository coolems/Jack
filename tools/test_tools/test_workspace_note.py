"""Regression coverage for the per-workspace "info" note (2026-11-27, user request).

Every workspace can carry one free-text note, edited from the sidebar row menu
("Edit workspace info"). The note lives in conversations.workspace_note - a column of the
workspace's OWN row - which is what makes the two promises hold with no extra machinery:

  * saving is a plain save of the workspace record (no side table to keep in sync);
  * deleting the workspace deletes the note with it (no orphan note row survives).

Covered here:
  * database.py adds the column to a fresh schema and to a legacy DB (NULL for old rows);
  * GET /{id}/note returns "" for a NULL note and the CURRENT title (so the editor can name
    the workspace it is about to write to);
  * POST /{id}/note stores the whole text, "" clears it, and a body that is not a string or
    is oversized is refused BEFORE the write (a malformed body must not blank a real note);
  * both routes are ownership-guarded like every other per-workspace route;
  * the UI contract: the sidebar menu entry, the modal markup/script and the endpoint URL.
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
    unread_finished INTEGER DEFAULT 0,
    workspace_note TEXT DEFAULT NULL
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


def _load_conversations(monkeypatch, db, delete_rows=None):
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
    turn_ops.delete_conversation_rows = delete_rows or (
        lambda cursor, conv_id: {"turn_log": 0, "messages": 0, "conversations": 0}
    )
    turn_ops.truncate_turn_logs = lambda *a, **k: 0
    turn_ops.load_turn_logs = lambda conv_id: []
    ws_pkg.turn_ops = turn_ops
    monkeypatch.setitem(sys.modules, "app.websocket", ws_pkg)
    monkeypatch.setitem(sys.modules, "app.websocket.turn_ops", turn_ops)

    conversations = load_module("CLIENT/app/routers/conversations.py", "_conversations_note_test")
    agent = types.SimpleNamespace(get_name=lambda: "Jack")
    router = conversations.create_conversations_router(":memory:", "test-model", agent)
    return conversations, router


class _FakeRequest:
    pass


def _route(router, path_suffix, method):
    for r in router.routes:
        if not getattr(r, "path", "").endswith(path_suffix):
            continue
        if method in (getattr(r, "methods", None) or set()):
            return r
    raise AssertionError(f"route not found: {path_suffix} {method}")


def _db_with(*rows):
    """In-memory DB holding CONV_SCHEMA plus the given (id, title, ip, note) rows."""
    db = sqlite3.connect(":memory:")
    db.execute(CONV_SCHEMA)
    for conv_id, title, ip, note in rows:
        db.execute(
            "INSERT INTO conversations (id, title, ip_address, workspace_note) VALUES (?, ?, ?, ?)",
            (conv_id, title, ip, note),
        )
    db.commit()
    return db


# ===== migration =====


def test_migration_adds_workspace_note_to_legacy_db(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(db_path))
    legacy.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT, ip_address TEXT)")
    legacy.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT, role TEXT, content TEXT)")
    legacy.execute("INSERT INTO conversations (id, title, ip_address) VALUES ('oldwsaa', 'Old', '1.2.3.4')")
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

    database = load_module("CLIENT/app/database.py", "_database_note_test")
    database.init_db(str(db_path))

    check = sqlite3.connect(str(db_path))
    try:
        cols = [r[1] for r in check.execute("PRAGMA table_info(conversations)").fetchall()]
        assert "workspace_note" in cols
        # A workspace that never got a note stays NULL -> the UI opens it blank.
        assert check.execute("SELECT workspace_note FROM conversations WHERE id='oldwsaa'").fetchone()[0] is None
    finally:
        check.close()


# ===== GET /{id}/note =====


def test_get_note_reads_null_as_empty_and_returns_title(monkeypatch, quiet_security):
    db = _db_with(("ownwsaa", "Docs site", OWN_IP, None))

    _, router = _load_conversations(monkeypatch, db)
    route = _route(router, "/{conv_id}/note", "GET")
    result = asyncio.run(route.endpoint("ownwsaa", _FakeRequest()))

    assert result == {"id": "ownwsaa", "title": "Docs site", "note": ""}
    db.close()


def test_get_note_returns_stored_text(monkeypatch, quiet_security):
    db = _db_with(("ownwsaa", "Docs site", OWN_IP, "run the build with `make docs`"))

    _, router = _load_conversations(monkeypatch, db)
    route = _route(router, "/{conv_id}/note", "GET")
    result = asyncio.run(route.endpoint("ownwsaa", _FakeRequest()))

    assert result["note"] == "run the build with `make docs`"
    db.close()


# ===== POST /{id}/note =====


def test_post_note_saves_and_reads_back(monkeypatch, quiet_security):
    db = _db_with(("ownwsaa", "Docs site", OWN_IP, None))

    _, router = _load_conversations(monkeypatch, db)
    save = _route(router, "/{conv_id}/note", "POST")
    note = "Line one\n  indented line (keep whitespace)\n"
    result = asyncio.run(save.endpoint("ownwsaa", {"note": note}, _FakeRequest()))

    assert result == {"status": "saved", "length": len(note)}
    stored = db.execute("SELECT workspace_note FROM conversations WHERE id='ownwsaa'").fetchone()[0]
    assert stored == note       # saved verbatim, no trim (indentation is part of the note)

    read = _route(router, "/{conv_id}/note", "GET")
    assert asyncio.run(read.endpoint("ownwsaa", _FakeRequest()))["note"] == note
    db.close()


def test_post_note_empty_string_clears_it(monkeypatch, quiet_security):
    db = _db_with(("ownwsaa", "Docs site", OWN_IP, "old text"))

    _, router = _load_conversations(monkeypatch, db)
    save = _route(router, "/{conv_id}/note", "POST")
    asyncio.run(save.endpoint("ownwsaa", {"note": ""}, _FakeRequest()))

    assert db.execute("SELECT workspace_note FROM conversations WHERE id='ownwsaa'").fetchone()[0] == ""
    db.close()


def test_post_note_rejects_bad_bodies(monkeypatch, quiet_security):
    db = _db_with(("ownwsaa", "Docs site", OWN_IP, "keep me"))

    conversations, router = _load_conversations(monkeypatch, db)
    save = _route(router, "/{conv_id}/note", "POST")

    with pytest.raises(HTTPException) as err:
        asyncio.run(save.endpoint("ownwsaa", {"note": 42}, _FakeRequest()))
    assert err.value.status_code == 422

    with pytest.raises(HTTPException) as err:
        asyncio.run(save.endpoint("ownwsaa", {}, _FakeRequest()))
    assert err.value.status_code == 422

    with pytest.raises(HTTPException) as err:
        asyncio.run(save.endpoint(
            "ownwsaa", {"note": "x" * (conversations.NOTE_MAX_CHARS + 1)}, _FakeRequest()))
    assert err.value.status_code == 422

    # Refused BEFORE the write: the real note is still there.
    assert db.execute("SELECT workspace_note FROM conversations WHERE id='ownwsaa'").fetchone()[0] == "keep me"
    db.close()


def test_note_routes_guard_foreign_and_unknown(monkeypatch, quiet_security):
    db = _db_with(("foreign", "Other", OTHER_IP, "secret"))

    _, router = _load_conversations(monkeypatch, db)
    read = _route(router, "/{conv_id}/note", "GET")
    save = _route(router, "/{conv_id}/note", "POST")

    with pytest.raises(HTTPException) as err:
        asyncio.run(read.endpoint("foreign", _FakeRequest()))
    assert err.value.status_code == 403

    with pytest.raises(HTTPException) as err:
        asyncio.run(save.endpoint("foreign", {"note": "pwned"}, _FakeRequest()))
    assert err.value.status_code == 403

    with pytest.raises(HTTPException) as err:
        asyncio.run(read.endpoint("nosuchw", _FakeRequest()))
    assert err.value.status_code == 404

    assert db.execute("SELECT workspace_note FROM conversations WHERE id='foreign'").fetchone()[0] == "secret"
    db.close()


# ===== the note dies with the workspace =====


def test_deleting_workspace_deletes_its_note(monkeypatch, quiet_security):
    """The note is a column of the workspace row, so the existing delete removes both."""
    db = _db_with(("ownwsaa", "Docs site", OWN_IP, "useful info"))

    def _delete_rows(cursor, conv_id):
        # Same order as turn_ops.delete_conversation_rows: children first, then the row.
        cursor.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        return {"turn_log": 0, "messages": 0, "conversations": 1}

    _, router = _load_conversations(monkeypatch, db, delete_rows=_delete_rows)
    delete = _route(router, "/api/conversations/{conv_id}", "DELETE")
    asyncio.run(delete.endpoint("ownwsaa", _FakeRequest()))

    assert db.execute("SELECT COUNT(*) FROM conversations WHERE id='ownwsaa'").fetchone()[0] == 0
    db.close()


# ===== UI static contract =====


def test_static_contracts():
    cm = (_REPO_ROOT / "CLIENT/static/ui/conversation-manager.js").read_text(encoding="utf-8")
    assert "'Edit workspace info'" in cm
    assert "WorkspaceNote.open(id)" in cm

    note = (_REPO_ROOT / "CLIENT/static/ui/workspace-note.js").read_text(encoding="utf-8")
    assert "/note`" in note                      # GET/POST target
    assert "JSON.stringify({ note })" in note    # the saved payload
    assert "WORKSPACE_NOTE_MAX = 20000" in note  # mirrors NOTE_MAX_CHARS

    page = (_REPO_ROOT / "CLIENT/index.html").read_text(encoding="utf-8")
    assert 'id="workspaceNoteModal"' in page
    assert 'id="workspaceNoteInput"' in page
    assert 'id="workspaceNoteTarget"' in page    # names the workspace being edited
    assert "/static/ui/workspace-note.js" in page

    conv = (_REPO_ROOT / "CLIENT/app/routers/conversations.py").read_text(encoding="utf-8")
    assert "/{conv_id}/note" in conv
    assert "workspace_note" in conv

    database = (_REPO_ROOT / "CLIENT/app/database.py").read_text(encoding="utf-8")
    assert "workspace_note" in database
