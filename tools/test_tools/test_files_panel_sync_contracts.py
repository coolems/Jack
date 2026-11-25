"""Files panel <-> file tool contracts (2026-11-28).

Locks in the two behaviours the panel owes the loop, so a later refactor cannot drop them
silently:

  * "File tools sync" ON  -> read/write/edit/insert calls REVEAL their file in the panel.
  * independent of it     -> every call that CHANGES the tree (write/edit/insert/delete AND
                             move/rename) makes the panel re-read the folder it shows.
"""
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYNC = _REPO_ROOT / "CLIENT/static/ui/files/file-tools-sync.js"


def _source():
    return _SYNC.read_text(encoding="utf-8")


def _list_constant(name):
    """The JS array literal assigned to `name`, as a set of strings."""
    text = _source()
    start = text.index(name)
    body = text[text.index("[", start):text.index("]", start)]
    return {part.strip().strip("'\"") for part in body.strip("[]").split(",") if part.strip()}


def test_tree_changing_tools_refresh_the_panel():
    changing = _list_constant("FILE_TOOLS_TREE_CHANGING_TOOLS")
    # move/rename change the tree in TWO places, so the panel must re-read as well.
    assert {"write_file", "edit_file", "insert_lines", "delete_file", "move_file", "rename_file"} <= changing


def test_sync_jump_only_for_tools_that_name_one_file():
    starts = _list_constant("FILE_TOOLS_SYNC_START_TOOLS")
    assert {"read_file", "write_file", "edit_file", "insert_lines"} == starts


def test_refresh_ignores_replays_and_closed_panels():
    text = _source()
    assert "isReplay" in text
    assert "COOLEMS.filesPanelOpen" in text
    # A delete has nothing to select, and a failed call never jumps.
    assert "delete_file'" in text and "success !== false" in text
