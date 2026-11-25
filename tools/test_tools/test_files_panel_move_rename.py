"""Files panel MOVE + RENAME (2026-11-29).

The panel can now move a row (drag it onto a folder, or "Move to folder..." in the row
menu) and rename a row ("Rename" in the menu, or a double click on its name). Two halves
are covered here:

  * POST /api/move and POST /api/rename - the backend. Both stay inside the authorized
    workspace folder, never overwrite, never touch the workspace root itself, refuse a
    folder moved into itself, and move a folder WITH its whole subtree.
  * the static contract - the menu carries Rename + Move, the drag payload is the panel's
    own marker (so a desktop drop stays an upload), and the move/rename tools of the agent
    keep working on folders.
"""
import asyncio
import importlib
import os
import shutil
from pathlib import Path

import pytest
from fastapi import APIRouter, HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Autouse fixture: CLIENT modules are imported as `app` (see _client_imports for why).
from tools.test_tools._client_imports import client_app_scope, load_module  # noqa: F401


@pytest.fixture
def quiet_security(monkeypatch):
    """Real security_events module with the JSONL write stubbed (no test pollution)."""
    from app import security_events

    security_events.clear()
    monkeypatch.setattr(security_events, "_write", lambda event: None)
    yield security_events
    security_events.clear()


@pytest.fixture
def routes(quiet_security):
    """The write endpoints, registered on a bare router, with the workspace published."""
    module = load_module("CLIENT/app/routers/files/write_endpoints.py", "app.routers.files._move_test")
    router = APIRouter()
    module.register_write_endpoints(router)
    by_path = {getattr(route, "path", ""): route for route in router.routes}
    return by_path


@pytest.fixture
def workspace(tmp_path):
    """A workspace folder published into the files-router ContextVar."""
    middleware = importlib.import_module("app.routers.files.middleware")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "readme.md").write_text("hello", encoding="utf-8")
    (root / "keep.txt").write_text("keep", encoding="utf-8")
    middleware._file_request_working_root.set(str(root))
    return root


def _move(routes, source, destination):
    return asyncio.run(routes["/api/move"].endpoint({"source": source, "destination_path": destination}))


def _rename(routes, path, new_name):
    return asyncio.run(routes["/api/rename"].endpoint({"path": path, "new_name": new_name}))


# ===== MOVE =====


def test_move_moves_a_file_into_a_folder(routes, workspace):
    result = _move(routes, "keep.txt", "docs")

    assert result == {"status": "moved", "path": "docs/keep.txt"}
    assert (workspace / "docs" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (workspace / "keep.txt").exists()


def test_move_a_folder_carries_its_whole_subtree(routes, workspace):
    (workspace / "docs" / "deep").mkdir()
    (workspace / "docs" / "deep" / "note.txt").write_text("n", encoding="utf-8")
    (workspace / "archive").mkdir()

    result = _move(routes, "docs", "archive")

    assert result["path"] == "archive/docs"
    assert (workspace / "archive" / "docs" / "deep" / "note.txt").read_text(encoding="utf-8") == "n"
    assert not (workspace / "docs").exists()


def test_move_to_the_workspace_root(routes, workspace):
    result = _move(routes, "docs/readme.md", "")

    assert result["path"] == "readme.md"
    assert (workspace / "readme.md").is_file()


def test_move_refuses_an_existing_target(routes, workspace):
    (workspace / "docs" / "keep.txt").write_text("other", encoding="utf-8")

    with pytest.raises(HTTPException) as err:
        _move(routes, "keep.txt", "docs")

    assert err.value.status_code == 409
    # Nothing was overwritten.
    assert (workspace / "docs" / "keep.txt").read_text(encoding="utf-8") == "other"
    assert (workspace / "keep.txt").is_file()


def test_move_refuses_a_folder_into_itself(routes, workspace):
    (workspace / "docs" / "inner").mkdir()

    for destination in ("docs", "docs/inner"):
        with pytest.raises(HTTPException) as err:
            _move(routes, "docs", destination)
        assert err.value.status_code == 400


def test_move_refuses_the_workspace_root_as_source(routes, workspace, quiet_security):
    with pytest.raises(HTTPException) as err:
        _move(routes, ".", "docs")

    assert err.value.status_code == 400
    assert (workspace / "docs").is_dir()
    assert "working_root_move_blocked" in [event["kind"] for event in quiet_security.recent(10)]


def test_move_refuses_a_path_outside_the_workspace(routes, workspace, quiet_security):
    with pytest.raises(HTTPException) as err:
        _move(routes, "../outside.txt", "docs")

    assert err.value.status_code == 400
    assert "path_escape" in [event["kind"] for event in quiet_security.recent(10)]


def test_move_reports_a_missing_source(routes, workspace):
    with pytest.raises(HTTPException) as err:
        _move(routes, "gone.txt", "docs")

    assert err.value.status_code == 404


def test_move_refuses_a_destination_that_is_a_file(routes, workspace):
    with pytest.raises(HTTPException) as err:
        _move(routes, "docs/readme.md", "keep.txt")

    assert err.value.status_code == 400


def test_move_refuses_a_blank_source(routes, workspace):
    with pytest.raises(HTTPException) as err:
        _move(routes, "   ", "docs")

    assert err.value.status_code == 400


# ===== RENAME =====


def test_rename_renames_a_file(routes, workspace):
    result = _rename(routes, "keep.txt", "kept.txt")

    assert result == {"status": "renamed", "path": "kept.txt"}
    assert (workspace / "kept.txt").read_text(encoding="utf-8") == "keep"
    assert not (workspace / "keep.txt").exists()


def test_rename_renames_a_folder(routes, workspace):
    result = _rename(routes, "docs", "documents")

    assert result["path"] == "documents"
    assert (workspace / "documents" / "readme.md").is_file()


def test_rename_refuses_a_separator_in_the_new_name(routes, workspace):
    for name in ("../escape.txt", "sub/name.txt", "sub\\name.txt", "..", "."):
        with pytest.raises(HTTPException) as err:
            _rename(routes, "keep.txt", name)
        assert err.value.status_code == 400
    assert (workspace / "keep.txt").is_file()


def test_rename_refuses_an_existing_target(routes, workspace):
    # The target must be in the SAME folder as the source (keep.txt is at the root).
    (workspace / "other.txt").write_text("other", encoding="utf-8")

    with pytest.raises(HTTPException) as err:
        _rename(routes, "keep.txt", "other.txt")

    assert err.value.status_code == 409
    assert (workspace / "keep.txt").is_file()
    assert (workspace / "other.txt").read_text(encoding="utf-8") == "other"


def test_rename_reports_a_missing_path(routes, workspace):
    with pytest.raises(HTTPException) as err:
        _rename(routes, "gone.txt", "other.txt")

    assert err.value.status_code == 404


def test_rename_refuses_the_workspace_root(routes, workspace):
    with pytest.raises(HTTPException) as err:
        _rename(routes, ".", "renamed")

    assert err.value.status_code == 400


def test_rename_allows_a_case_only_change(routes, workspace):
    """On a case-insensitive filesystem the target 'exists' because it IS the source."""
    result = _rename(routes, "keep.txt", "KEEP.txt")

    assert result["path"] == "KEEP.txt"
    assert [p.name for p in workspace.iterdir() if p.name.lower() == "keep.txt"] == ["KEEP.txt"]


# ===== AGENT TOOLS (folders) =====


def test_agent_move_file_moves_a_folder(tmp_path, monkeypatch):
    tools_utils = importlib.import_module("tools.utils")
    monkeypatch.setattr(tools_utils, "get_working_root", lambda: str(tmp_path))
    move_tool = load_module("tools/file_tools/move_file.py", "tools.file_tools._move_folder_test")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "dst").mkdir()

    result = move_tool.move_file("src", "dst")

    assert result.startswith("Success")
    assert (tmp_path / "dst" / "src" / "a.txt").is_file()


def test_agent_move_file_refuses_a_folder_into_itself(tmp_path, monkeypatch):
    tools_utils = importlib.import_module("tools.utils")
    monkeypatch.setattr(tools_utils, "get_working_root", lambda: str(tmp_path))
    move_tool = load_module("tools/file_tools/move_file.py", "tools.file_tools._move_self_test")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "inner").mkdir()

    assert "into itself" in move_tool.move_file("src", "src/inner")
    assert (tmp_path / "src" / "inner").is_dir()


def test_agent_rename_file_renames_a_folder(tmp_path, monkeypatch):
    tools_utils = importlib.import_module("tools.utils")
    monkeypatch.setattr(tools_utils, "get_working_root", lambda: str(tmp_path))
    rename_tool = load_module("tools/file_tools/rename_file.py", "tools.file_tools._rename_folder_test")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("a", encoding="utf-8")

    result = rename_tool.rename_file("src", "renamed")

    assert result.startswith("Success")
    assert (tmp_path / "renamed" / "a.txt").is_file()


# ===== STATIC CONTRACT =====


def test_context_menu_offers_rename_and_move():
    script = (_REPO_ROOT / "CLIENT/static/ui/files/file-context-menu.js").read_text(encoding="utf-8")
    assert "['Rename'" in script
    assert "['Move to folder...'" in script
    assert "inlineRenameFileRow" in script
    assert "moveFileItemToTypedFolder" in script


def test_panel_actions_call_the_two_endpoints():
    script = (_REPO_ROOT / "CLIENT/static/ui/files/file-actions.js").read_text(encoding="utf-8")
    assert "'/api/move'" in script
    assert "'/api/rename'" in script
    # The panel re-reads the tree after a change, and the rename is inline.
    assert "refreshFilePanelAfterFileChange" in script
    assert "file-rename-input" in script


def test_internal_drag_is_marked_and_moves():
    actions = (_REPO_ROOT / "CLIENT/static/ui/files/tree-actions.js").read_text(encoding="utf-8")
    drop = (_REPO_ROOT / "CLIENT/static/ui/files/drop-to-folder.js").read_text(encoding="utf-8")
    # The panel's own drag marker is what separates a MOVE from a desktop UPLOAD.
    assert "setData('coolems-file-drag', 'true')" in actions
    assert "copyMove" in actions
    assert "fileActionIsInternalDrag" in drop
    assert "moveFileItem(source, destination)" in drop
    # Dropping on the empty area of the list means the workspace root.
    assert "root-move-target" in drop
    css = (_REPO_ROOT / "CLIENT/static/ui/css/02-tree-preview-editor.css").read_text(encoding="utf-8")
    assert ".files-list.root-move-target" in css


def test_assets_are_loaded_in_order():
    index = (_REPO_ROOT / "CLIENT/index.html").read_text(encoding="utf-8")
    actions_at = index.index("/static/ui/files/file-actions.js")
    drop_at = index.index("/static/ui/files/drop-to-folder.js")
    menu_at = index.index("/static/ui/files/file-context-menu.js")
    assert actions_at < drop_at < menu_at


def test_rename_input_is_styled():
    css = (_REPO_ROOT / "CLIENT/static/ui/css/02-tree-preview-editor.css").read_text(encoding="utf-8")
    assert ".file-rename-input" in css
