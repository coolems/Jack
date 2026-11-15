"""Regression tests for the lint pass added to run_tests() (2026-11-15).

Pins the contract that makes the lint report trustworthy:
  * a defect IS reported (ruff rc=1 -> status 'fail', finding lines present);
  * a clean tree is 'pass', and a missing ruff is 'skip' - never 'pass';
  * a project with its own ruff config keeps its per-file ignores (only
    --extend-select is passed, never --config/--isolated);
  * a project WITHOUT a config is linted --isolated, so an ancestor repo's
    ruff.toml cannot silently decide what gets checked;
  * the combined verdict fails when lint fails, and the lint block survives
    every early-return path (no tests collected / runner exception).

Run:  python -m pytest tools/test_tools/test_run_tests_lint.py -v
"""

import importlib
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

rt = importlib.import_module("tools.test_tools.run_tests")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _project(tmp_path, files):
    """A throwaway project: src files + a tests/ folder with one collectable test."""
    root = os.path.join(str(tmp_path), "proj")
    for rel, text in files.items():
        _write(os.path.join(root, rel), text)
    _write(os.path.join(root, "tests", "test_ok.py"), "def test_ok():\n    assert 1 + 1 == 2\n")
    return root


def test_rules_are_defect_only():
    """Style rules must not be selected - this runs on any user project."""
    codes = rt._LINT_RULES.split(",")
    assert set(codes) == {"E9", "F821", "F811", "F601", "F841", "B006"}
    for style in ("E501", "I001", "UP", "BLE001", "S110"):
        assert not any(c.startswith(style) for c in codes)


def test_repo_root_is_clean():
    """The repo itself must stay lint-clean under the shipped ruff.toml."""
    status, engine, body = rt._run_lint(_REPO_ROOT)
    assert status == "pass", f"lint findings in the repo:\n{body}"
    assert "project config" in engine


def test_defects_are_reported(tmp_path):
    root = _project(tmp_path, {
        "src/bad.py": (
            "def compute(items=[]):\n"
            "    unused = 1\n"
            "    table = {'a': 1, 'a': 2}\n"
            "    return missing_helper(items, table)\n"
        ),
    })
    status, engine, body = rt._run_lint(root)
    assert status == "fail", body
    assert "isolated" in engine  # no config of its own
    for code in ("B006", "F841", "F601", "F821"):
        assert code in body, f"{code} not reported:\n{body}"


def test_project_config_is_honoured(tmp_path):
    """Own ruff.toml: its per-file ignore applies, defect rules still added."""
    root = _project(tmp_path, {
        "ruff.toml": '[lint]\nselect = ["E9"]\n[lint.per-file-ignores]\n"src/facade.py" = ["F821"]\n',
        "src/facade.py": "x = not_defined_anywhere\n",
        "src/bad.py": "def f(a=[]):\n    return a\n",
    })
    status, engine, body = rt._run_lint(root)
    assert status == "fail", body
    assert "project config" in engine
    assert "B006" in body           # defect rule added on top of the project's own
    assert "facade.py" not in body  # project's per-file ignore respected


def test_missing_ruff_is_skip_not_pass(monkeypatch):
    monkeypatch.setattr(rt, "_resolve_ruff", lambda root: (None, ""))
    status, engine, body = rt._run_lint(_REPO_ROOT)
    assert status == "skip"
    assert "pip install ruff" in body
    parts = rt._lint_section(status, engine, body)
    assert "**Status:** [SKIP]" in parts


def test_lint_cmd_shape():
    prefix = ["ruff", "check"]
    with_cfg = rt._lint_cmd(prefix, _REPO_ROOT)
    assert "--extend-select" in with_cfg and "--isolated" not in with_cfg
    assert "--config" not in with_cfg and "--fix" not in with_cfg
    without_cfg = rt._lint_cmd(prefix, os.path.join(_REPO_ROOT, ".temp"))
    assert "--isolated" in without_cfg and "--select" in without_cfg


def test_report_combines_lint_and_tests(tmp_path):
    """Full tool call: lint block first, verdict line last, lint=False disables."""
    root = _project(tmp_path, {"src/bad.py": "y = undefined_thing\n"})
    report = rt.run_tests(project_root=root, verbose=False)
    assert report.index("## Lint Results") < report.index("## Test Results")
    assert "**Status:** [FAIL]" in report.split("## Test Results")[0]
    assert report.rstrip().endswith("**Overall:** [FAIL] (lint: fail, tests: pass)")

    clean = rt.run_tests(project_root=root, verbose=False, lint=False)
    assert "**Status:** [DISABLED]" in clean
    assert clean.rstrip().endswith("**Overall:** [PASS] (lint: disabled, tests: pass)")


def test_lint_block_survives_early_returns(monkeypatch, tmp_path):
    """rc=5 and a runner crash must still carry the lint verdict."""
    root = _project(tmp_path, {"src/bad.py": "y = undefined_thing\n"})
    real = rt.subprocess.run

    def fake(cmd, **kw):
        if "-m" in cmd and "pytest" in cmd:
            class R:
                returncode, stdout, stderr = 5, "", ""
            return R()
        return real(cmd, **kw)

    monkeypatch.setattr(rt.subprocess, "run", fake)
    out = rt.run_tests(project_root=root, verbose=False)
    assert "## Lint Results" in out
    assert "No tests were collected" in out
    assert out.rstrip().endswith("**Overall:** [FAIL] (lint: fail, tests: fail)")

    def boom(cmd, **kw):
        if "-m" in cmd and "pytest" in cmd:
            raise RuntimeError("simulated runner crash")
        return real(cmd, **kw)

    monkeypatch.setattr(rt.subprocess, "run", boom)
    out2 = rt.run_tests(project_root=root, verbose=False)
    assert "## Lint Results" in out2 and "simulated runner crash" in out2
