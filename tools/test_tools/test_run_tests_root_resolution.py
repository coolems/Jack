"""Regression coverage for run_tests project-root resolution (2026-11-24).

The incident: run_tests() anchored on a DERIVED root. get_working_root() silently
substitutes the project root when no workspace root is published, and on a CLIENT
that module lives in memory, so the substitution was a synthetic path inside the
CLIENT tree - a run then targeted ...\\CLIENT\\tests (which does not exist) while
a relative project_root="tools/test_tools" got joined to that same wrong anchor.

The contract pinned here:
  * _published_working_root() returns the PUBLISHED root or None - never a
    substituted default;
  * an explicit project_root is absolute as given, or relative to the published
    root (process CWD only when nothing was published) - never joined to a
    derived anchor;
  * with no published root, a CWD sitting inside a CLIENT tree is NOT adopted
    (no __file__/CWD walk-up while another tree is in play) - the run fails
    loudly instead of testing the wrong tree.

CLIENT simulation: the CLIENT exec's tool source in its own globals, so the
injected get_published_working_root global is the authoritative source there.
These tests set that global (and, where noted, a STALE tools.utils that lacks
the new function - the real CLIENT state until it redeploys) to reproduce the
incident shape exactly.
"""
import os
import sys
from types import ModuleType

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.test_tools._client_imports import client_app_scope  # noqa: F401  (autouse, keeps `app` on the CLIENT tree)

import tools.test_tools.run_tests as rt
from tools.utils import get_published_working_root, set_current_working_root


def _make_client_tree(base, name):
    """A fake CLIENT tree: <base>/<name>/CLIENT/tools/test_tools/test_x.py."""
    client_root = os.path.join(base, name, "CLIENT")
    suite = os.path.join(client_root, "tools", "test_tools")
    os.makedirs(suite)
    with open(os.path.join(suite, "test_x.py"), "w", encoding="utf-8") as fh:
        fh.write("def test_x():\n    assert True\n")
    return client_root


def _stale_tools_utils(monkeypatch, project_root):
    """Install a tools.utils that lacks get_published_working_root - the state of
    the CLIENT's in-memory copy until it redeploys. Returns the module."""
    stale = ModuleType("tools.utils")
    stale.get_working_root = lambda: project_root  # the silent-substitution trap
    monkeypatch.setitem(sys.modules, "tools.utils", stale)
    return stale


def _set_injected_published(monkeypatch, value):
    """Publish *value* the way the CLIENT loader injects it: as a bare global of
    the tool module. Restored after the test."""
    monkeypatch.setitem(rt.__dict__, "get_published_working_root", lambda: value)


def test_published_root_is_never_substituted(tmp_path, monkeypatch):
    """No published root -> None (a fact), not the project root (a guess)."""
    set_current_working_root(None)
    try:
        assert get_published_working_root() is None
        assert rt._published_working_root() is None

        target = tmp_path / "ws"
        target.mkdir()
        set_current_working_root(str(target))
        assert get_published_working_root() == str(target)
        assert rt._published_working_root() == str(target)
    finally:
        set_current_working_root(None)


def test_explicit_relative_resolves_against_the_published_root(tmp_path, monkeypatch):
    """A relative project_root must land inside the workspace, not in a derived tree.

    The incident: project_root="tools/test_tools" got joined to a derived anchor
    (...\\CLIENT) instead of the workspace. CLIENT shape: the injected global is
    the source of truth and the in-memory tools.utils is STALE (lacks the new
    function) - the resolution must not touch it at all.
    """
    workspace = tmp_path / "ws"
    (workspace / "proj" / "tests").mkdir(parents=True)
    with open(str(workspace / "proj" / "tests" / "test_a.py"), "w", encoding="utf-8") as fh:
        fh.write("def test_a():\n    assert True\n")
    stale = _stale_tools_utils(monkeypatch, str(tmp_path / "derived"))
    consulted = {"n": 0}
    stale.get_working_root = lambda: (consulted.__setitem__("n", consulted["n"] + 1),
                                      str(tmp_path / "derived"))[1]
    _set_injected_published(monkeypatch, str(workspace))
    try:
        root, how, suite = rt._resolve_project_root("proj")
        assert root == str(workspace / "proj")
        assert suite == "tests"
        assert "resolved against" in how
        assert consulted["n"] == 0  # the substitution trap was never called
    finally:
        set_current_working_root(None)


def test_explicit_absolute_is_used_as_given(tmp_path, monkeypatch):
    """An absolute project_root is never re-joined to any anchor."""
    other = tmp_path / "other"
    (other / "tests").mkdir(parents=True)
    with open(str(other / "tests" / "test_b.py"), "w", encoding="utf-8") as fh:
        fh.write("def test_b():\n    assert True\n")
    set_current_working_root(str(tmp_path))
    try:
        root, how, suite = rt._resolve_project_root(str(other))
        assert root == str(other)
        assert suite == "tests"
        assert "resolved against" not in how
    finally:
        set_current_working_root(None)


def test_no_published_root_inside_a_client_tree_fails_loudly(tmp_path, monkeypatch):
    """The incident shape: CWD inside a CLIENT tree, no published root, module
    installed in memory (synthetic __file__).

    Old code anchored on the module path / walked up the CWD into the CLIENT
    tree. Now: no published root and no on-disk module -> no target, and the
    failure message says exactly that.
    """
    client_root = _make_client_tree(str(tmp_path), "clientA")
    _stale_tools_utils(monkeypatch, str(tmp_path / "derived"))
    _set_injected_published(monkeypatch, None)
    real_file = rt.__file__
    monkeypatch.setattr(rt, "__file__", "<remote_tool_run_tests>")
    monkeypatch.chdir(client_root)
    try:
        root, why, _ = rt._resolve_project_root(None)
        assert root is None
        assert "no working root is configured" in why
    finally:
        set_current_working_root(None)
        monkeypatch.setattr(rt, "__file__", real_file)


def test_no_published_root_relative_explicit_uses_cwd(tmp_path, monkeypatch):
    """Without a published root a relative project_root resolves against the CWD."""
    base = tmp_path / "work"
    (base / "proj" / "tests").mkdir(parents=True)
    with open(str(base / "proj" / "tests" / "test_c.py"), "w", encoding="utf-8") as fh:
        fh.write("def test_c():\n    assert True\n")
    set_current_working_root(None)
    monkeypatch.chdir(str(base))
    try:
        root, how, suite = rt._resolve_project_root("proj")
        assert root == str(base / "proj")
        assert suite == "tests"
        assert "resolved against" in how
    finally:
        set_current_working_root(None)


def test_published_root_beats_cwd_walk_up(tmp_path, monkeypatch):
    """A published root is the ONLY anchor: the CWD tree is not consulted at all."""
    workspace = tmp_path / "ws"
    (workspace / "tests").mkdir(parents=True)
    with open(str(workspace / "tests" / "test_d.py"), "w", encoding="utf-8") as fh:
        fh.write("def test_d():\n    assert True\n")
    # CWD sits inside a fake CLIENT tree that also ships a suite - it must be ignored.
    client_root = _make_client_tree(str(tmp_path), "clientB")
    set_current_working_root(str(workspace))
    monkeypatch.chdir(client_root)
    try:
        root, how, suite = rt._resolve_project_root(None)
        assert root == str(workspace)
        assert suite == "tests"
        assert "configured working root" in how
    finally:
        set_current_working_root(None)
