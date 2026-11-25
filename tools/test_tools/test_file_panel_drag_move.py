"""Files panel: dragging a row onto a folder moves it (2026-11-30).

The regression that started this file: a FILE row could be dragged onto a folder, but a
FOLDER row could not be dragged at all - tree-render-items.js / tree-render-children.js
gave folder rows neither `draggable="true"` nor `ondragstart`, so the browser started no
drag and the gesture was silently dead ("drag to move does nothing"). The drop path could
also fail silently in other ways.

Two layers are covered:

  * static contract - both renderers mark folder rows draggable with the SAME dragstart
    handler the file rows use, and the drop path never returns without telling the user.
  * live drag - the REAL panel modules driven by a real Chromium: mouse-driven HTML5
    drags, with app.js's global drag block installed exactly as the page installs it.
    Skipped when Playwright or a browser binary is unavailable.
"""
import json
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MODULES = _REPO_ROOT / "CLIENT" / "static" / "ui" / "files"
_RENDERERS = ["tree-render-items.js", "tree-render-children.js"]


# ============================================================ static contract

def _folder_branch(filename):
    """The `if (item.type === 'folder') { ... } else {` block of a renderer."""
    text = (_MODULES / filename).read_text(encoding="utf-8")
    start = text.index("if (item.type === 'folder') {")
    end = text.index("} else {", start)
    return text[start:end]


def _file_branch(filename):
    text = (_MODULES / filename).read_text(encoding="utf-8")
    start = text.index("} else {", text.index("if (item.type === 'folder') {")) + len("} else {")
    return text[start:]


@pytest.mark.parametrize("filename", _RENDERERS)
def test_folder_rows_are_draggable(filename):
    """A folder row must start a drag exactly like a file row does."""
    branch = _folder_branch(filename)
    assert 'draggable="true"' in branch, f"{filename}: folder rows are not draggable"
    assert 'ondragstart="handleFileDragStart(' in branch, f"{filename}: folder rows have no dragstart"
    assert "tree-folder-draggable" in branch, f"{filename}: no class for the grab cursor"


@pytest.mark.parametrize("filename", _RENDERERS)
def test_file_rows_keep_their_drag(filename):
    """Guard: the folder fix must not be achieved by dropping the file one."""
    branch = _file_branch(filename)
    assert 'draggable="true"' in branch
    assert 'ondragstart="handleFileDragStart(' in branch


@pytest.mark.parametrize("filename", _RENDERERS)
def test_folder_and_file_drag_handlers_agree(filename):
    """Both branches pass the same (path, name) shape to handleFileDragStart."""
    folder = _folder_branch(filename)
    file_ = _file_branch(filename)
    call = re.compile(r"handleFileDragStart\(event, '\$\{escapeJsAttr\(itemPath\)\}', '\$\{escapeJsAttr\(item\.name\)\}'\)")
    assert call.search(folder), folder[:400]
    assert call.search(file_), file_[:400]


def test_drop_never_fails_silently():
    """An unreadable drag payload used to `return` with no feedback at all."""
    source = (_MODULES / "drop-to-folder.js").read_text(encoding="utf-8")
    assert "Could not read the dragged item" in source
    assert "Move is unavailable" in source
    assert "await moveFileItem(source, destination);" in source


def test_refused_move_paints_no_root_target():
    """Passing '' to paintMoveTarget painted the ROOT outline for a refused move."""
    source = (_MODULES / "drop-to-folder.js").read_text(encoding="utf-8")
    assert "if (allowed) paintMoveTarget(target);" in source
    assert "else clearMoveTarget();" in source
    assert "paintMoveTarget(allowed ? target : '')" not in source


def test_move_failure_reasons_reach_the_user():
    source = (_MODULES / "file-actions.js").read_text(encoding="utf-8")
    assert "Could not read the dragged item" in source
    assert "Another file operation is still running" in source
    assert "A name is required" in source


def test_move_and_rename_remap_panel_state():
    """expandedFolders / selectedItems are keyed by path - they must follow the move."""
    source = (_MODULES / "file-actions.js").read_text(encoding="utf-8")
    assert "function fileActionRemapTreeState(oldPath, newPath)" in source
    move = source.index("async function moveFileItem")
    rename = source.index("async function renameFileItem")
    assert "fileActionRemapTreeState(source, result && result.path);" in source[move:rename]
    assert "fileActionRemapTreeState(path, fileActionJoin(fileActionParent(path), name));" in source[rename:]


def test_both_row_kinds_share_the_grab_cursor():
    css = (_REPO_ROOT / "CLIENT" / "static" / "ui" / "css" / "02-tree-preview-editor.css").read_text(encoding="utf-8")
    assert ".tree-file-draggable, .tree-folder-draggable { cursor: grab; }" in css


# ============================================================ live drag (browser)

_HARNESS = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>drag harness</title>
<style>
  .files-list { width: 320px; height: 400px; overflow: auto; border: 1px solid #ccc; }
  .tree-item-main { display: flex; align-items: center; gap: 4px; padding: 4px; }
</style></head>
<body>
<div class="files-list" id="filesList"></div>
<script>
window.__log = [];
function __rec(kind, data) { window.__log.push(Object.assign({kind: kind}, data)); }
%(escapes)s
function getFileIcon() { return 'F'; }
function formatFileSize(n) { return (n || 0) + ' B'; }
function showNotification(msg, type) { __rec('notify', {msg: msg, type: type}); }
function currentConvId() { return 'testconv'; }
function loadTree(path) { __rec('loadTree', {path: path}); return Promise.resolve(true); }
function previewFile() {}
function toggleFolder() {}
window.fetch = function (url, opts) {
    __rec('fetch', {url: String(url), method: (opts && opts.method) || 'GET', body: (opts && opts.body) || ''});
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ok: true, path: 'moved/x'}); } });
};
// app.js installs these four on document.body (DOMContentLoaded) - same here.
['dragenter', 'dragover', 'dragleave', 'drop'].forEach(function (t) {
    document.body.addEventListener(t, function (e) { e.preventDefault(); e.stopPropagation(); }, false);
});
</script>
%(scripts)s
<script>
// Every drag event that reaches the document (capture phase), so "no drag was started at
// all" (the reported bug) is distinguishable from "a drag started but the drop was lost".
['dragstart', 'dragenter', 'dragover', 'dragleave', 'drop', 'dragend'].forEach(function (t) {
    document.addEventListener(t, function (e) {
        var types = [];
        try { types = e.dataTransfer ? Array.from(e.dataTransfer.types) : []; } catch (err) { types = ['<err>']; }
        __rec('dom:' + t, {types: types});
    }, true);
});
</script>
</body></html>
"""


def _js_function(source, name):
    """Extract `function <name>(...) { ... }` from real source (no copy-paste of helpers)."""
    start = source.index("function " + name + "(")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def _write_harness(tmp_path):
    app_js = (_REPO_ROOT / "CLIENT" / "static" / "ui" / "app.js").read_text(encoding="utf-8")
    escapes = "\n".join(_js_function(app_js, name) for name in ("escapeAttr", "escapeJsAttr"))
    order = ["files-state.js", "tree-render-items.js", "tree-render-children.js",
             "tree-actions.js", "file-actions.js", "drop-to-folder.js"]
    scripts = "\n".join(f'<script src="{(_MODULES / name).as_uri()}"></script>' for name in order)
    page = tmp_path / "harness.html"
    page.write_text(_HARNESS % {"escapes": escapes, "scripts": scripts}, encoding="utf-8")
    return page


@pytest.fixture(scope="module")
def browser_page(tmp_path_factory):
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(headless=True)
        except Exception as exc:                     # no browser binary in this environment
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(_write_harness(tmp_path_factory.mktemp("drag")).as_uri())
        page.wait_for_timeout(200)
        page.evaluate("""items => renderTreeItems(items, '')""", [
            {"name": "code.py", "type": "file", "size": 120},
            {"name": "folderA", "type": "folder"},
            {"name": "folderB", "type": "folder"},
        ])
        page.evaluate("""() => {
            const row = document.querySelector('.tree-folder[data-path="folderA"]');
            const box = document.createElement('div');
            box.className = 'tree-children';
            box.innerHTML = renderChildrenItems([{name: 'sub', type: 'folder'}], 'folderA');
            row.appendChild(box);
        }""")
        page.__dict__["_errors"] = errors
        yield page
        browser.close()


def _drag(page, source, target):
    page.evaluate("window.__log = []")
    page.drag_and_drop(source, target)
    page.wait_for_timeout(250)
    log = page.evaluate("window.__log")
    events = [e["kind"] for e in log if e["kind"].startswith("dom:")]
    moves = [e for e in log if e["kind"] == "fetch" and "/api/move" in e["url"]]
    return events, moves


def test_live_drag_file_onto_folder(browser_page):
    events, moves = _drag(browser_page, '[data-path="code.py"] > .tree-item-main',
                          '[data-path="folderA"] > .tree-item-main')
    assert events, "the browser started no drag for a file row"
    assert [json.loads(m["body"]) for m in moves] == [
        {"source": "code.py", "destination_path": "folderA"}]


def test_live_drag_folder_onto_folder(browser_page):
    """The reported bug: this produced NO drag events at all before the fix."""
    events, moves = _drag(browser_page, '[data-path="folderB"] > .tree-item-main',
                          '[data-path="folderA"] > .tree-item-main')
    assert events, "the browser started no drag for a folder row"
    assert [json.loads(m["body"]) for m in moves] == [
        {"source": "folderB", "destination_path": "folderA"}]


def test_live_drag_nested_folder_onto_root(browser_page):
    events, moves = _drag(browser_page, '[data-path="folderA/sub"] > .tree-item-main', "#filesList")
    assert events
    assert [json.loads(m["body"]) for m in moves] == [
        {"source": "folderA/sub", "destination_path": ""}]


def test_live_drag_folder_into_its_own_subtree_is_refused(browser_page):
    events, moves = _drag(browser_page, '[data-path="folderA"] > .tree-item-main',
                          '[data-path="folderA/sub"] > .tree-item-main')
    assert events
    assert moves == []


def test_live_click_on_a_draggable_folder_row_still_opens_it(browser_page):
    browser_page.evaluate("window.__toggled = null; window.toggleFolder = p => { window.__toggled = p; };")
    browser_page.click('[data-path="folderB"] > .tree-item-main')
    browser_page.wait_for_timeout(100)
    assert browser_page.evaluate("window.__toggled") == "folderB"


def test_harness_page_had_no_script_errors(browser_page):
    assert browser_page.__dict__["_errors"] == []
