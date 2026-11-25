"""Files panel row context menu: Copy name / Copy full path / Show in explorer (2026-11-28).

The Files panel duplicates the workspace-row right-click menu for every tree row. Two halves
are covered here:

  * POST /api/reveal - the only backend piece. It must stay inside the authorized workspace
    folder, must refuse a missing target, must not spawn anything when the SERVER-delivered
    profile forbids subprocesses, and must reveal with the platform's own command
    (explorer.exe /select, open -R, xdg-open) instead of executing the file.
  * the static contract - the menu is delegated on #filesList (rows are re-rendered with
    innerHTML, so per-row listeners would vanish), carries the three labels and builds the
    full path from the workspace folder, not from the panel path.
"""
import asyncio
import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import APIRouter, HTTPException

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Autouse fixture: CLIENT modules are imported as `app` (see _client_imports for why).
from tools.test_tools._client_imports import client_app_scope, load_module  # noqa: F401

WS = "ownwsaa"


@pytest.fixture
def quiet_security(monkeypatch):
    """Real security_events module with the JSONL write stubbed (no test pollution)."""
    from app import security_events

    security_events.clear()
    monkeypatch.setattr(security_events, "_write", lambda event: None)
    yield security_events
    security_events.clear()


def _load_reveal(monkeypatch, allowed=True, reason="Subprocess execution is forbidden by your SERVER-side profile"):
    """Load special_endpoints.py with a stubbed SERVER-limitation gate."""
    keys = ModuleType("app.keys")
    keys.is_subprocess_allowed = lambda: (allowed, "" if allowed else reason)
    monkeypatch.setitem(sys.modules, "app.keys", keys)
    return load_module("CLIENT/app/routers/files/special_endpoints.py", "app.routers.files._reveal_test")


def _reveal_route(module):
    router = APIRouter()
    module.register_special_endpoints(router)
    for route in router.routes:
        if getattr(route, "path", "") == "/api/reveal":
            return route
    raise AssertionError("/api/reveal route not registered")


@pytest.fixture
def workspace(tmp_path, monkeypatch, quiet_security):
    """A workspace folder published into the files-router ContextVar + a Popen spy."""
    middleware = importlib.import_module("app.routers.files.middleware")
    root = tmp_path / "ws"
    root.mkdir()
    middleware._file_request_working_root.set(str(root))

    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)))
    return root, calls


def test_reveal_selects_a_file_in_windows_explorer(workspace, monkeypatch):
    root, calls = workspace
    module = _load_reveal(monkeypatch)
    target = root / "code.py"
    target.write_text("print(1)", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")

    result = asyncio.run(_reveal_route(module).endpoint(path="code.py"))

    assert result == {"status": "revealed", "path": "code.py"}
    assert len(calls) == 1
    argv = calls[0][0]
    assert argv[0].lower().endswith("explorer.exe")
    # ONE argv element, and the file itself is never handed to os.startfile().
    assert argv[1] == "/select," + str(target)


def test_reveal_selects_a_folder_in_windows_explorer(workspace, monkeypatch):
    root, calls = workspace
    module = _load_reveal(monkeypatch)
    (root / "app").mkdir()
    monkeypatch.setattr(sys, "platform", "win32")

    asyncio.run(_reveal_route(module).endpoint(path="app"))

    assert calls[0][0][1] == "/select," + str(root / "app")


def test_reveal_uses_open_r_on_macos(workspace, monkeypatch):
    root, calls = workspace
    module = _load_reveal(monkeypatch)
    target = root / "notes.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")

    asyncio.run(_reveal_route(module).endpoint(path="notes.txt"))

    assert calls[0][0] == ["open", "-R", str(target)]


def test_reveal_opens_the_containing_folder_on_linux(workspace, monkeypatch):
    root, calls = workspace
    module = _load_reveal(monkeypatch)
    (root / "sub").mkdir()
    target = root / "sub" / "a.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")

    asyncio.run(_reveal_route(module).endpoint(path="sub/a.txt"))

    # xdg-open has no select mode: the folder, never the file (which could be executable).
    assert calls[0][0] == ["xdg-open", str(root / "sub")]


def test_reveal_refuses_a_path_outside_the_workspace(workspace, monkeypatch, quiet_security):
    _, calls = workspace
    module = _load_reveal(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(HTTPException) as err:
        asyncio.run(_reveal_route(module).endpoint(path="../outside.txt"))

    assert err.value.status_code == 400
    assert calls == []
    assert "path_escape" in [event["kind"] for event in quiet_security.recent(10)]


def test_reveal_reports_a_vanished_target(workspace, monkeypatch):
    _, calls = workspace
    module = _load_reveal(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(HTTPException) as err:
        asyncio.run(_reveal_route(module).endpoint(path="gone.txt"))

    assert err.value.status_code == 404
    assert calls == []


def test_reveal_is_refused_when_the_profile_forbids_subprocesses(workspace, monkeypatch):
    _, calls = workspace
    module = _load_reveal(monkeypatch, allowed=False, reason="blocked by profile")
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(HTTPException) as err:
        asyncio.run(_reveal_route(module).endpoint(path=""))

    assert err.value.status_code == 403
    assert "blocked by profile" in err.value.detail
    assert calls == []


def test_reveal_without_a_path_reveals_the_workspace_folder(workspace, monkeypatch):
    root, calls = workspace
    module = _load_reveal(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")

    result = asyncio.run(_reveal_route(module).endpoint(path=""))

    assert result == {"status": "revealed", "path": "."}
    assert calls[0][0][1] == "/select," + str(root)


# ===== static contract =====


def test_menu_module_is_wired_to_the_whole_tree():
    script = (_REPO_ROOT / "CLIENT/static/ui/files/file-context-menu.js").read_text(encoding="utf-8")
    # Delegated on the container: one listener serves re-rendered rows AND nested folder
    # children (tree-render-items.js / tree-render-children.js both write into #filesList).
    assert "getElementById('filesList')" in script
    assert "list.addEventListener('contextmenu', showFileContextMenu)" in script
    assert "closest('.tree-item')" in script
    for label in ("Copy name", "Copy full path", "Show in explorer"):
        assert label in script
    assert "'/api/reveal'" in script
    # Full path = workspace folder + relative path, separator taken from the folder itself.
    assert "currentWorkingRoot" in script
    assert "/api/working_root" in script


def test_menu_assets_are_loaded_and_styled():
    index = (_REPO_ROOT / "CLIENT/index.html").read_text(encoding="utf-8")
    assert "/static/ui/files/file-context-menu.js" in index

    css = (_REPO_ROOT / "CLIENT/static/ui/css/06-responsive-conversations.css").read_text(encoding="utf-8")
    assert ".file-context-menu" in css
