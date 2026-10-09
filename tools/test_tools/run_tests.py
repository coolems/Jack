"""Run Tests Tool - Execute a test suite against the ACTIVE project root.

This tool runs all tests found in ``<root>/tests/`` and returns a summary of
results. Use this after any code changes to verify nothing is broken.

Usage:
    run_tests()                                  - Run the suite for the active project
    run_tests(test_file="test_python_exec")      - Run one specific test file
    run_tests(project_root="C:\\path\\to\\proj") - Explicitly target another project root

PER-PROJECT TESTS (2026-10-06)
------------------------------
The runner always targets the PROJECT that is currently being worked on, i.e.
the working root set in the CLIENT UI ("Working Folder" field), NOT whatever
tree this module happens to be imported from:

  * Put a ``tests/`` folder with test files inside your project (inside the
    working root) and call run_tests() - it will discover and run exactly that
    suite, in that folder. Each project therefore keeps its own test suite
    right next to its code (your idea, implemented).
  * A fresh project WITHOUT tests gets a loud [FAIL] explaining where to add
    them - the runner NEVER silently switches over to some other tree (e.g.
    Jack's own repo) just because one exists on disk or under the CWD.

RUNNERS (2026-10-06): pytest is preferred (it collects both plain ``def test_*``
functions and unittest.TestCase classes, with no package/import quirks); when
the target interpreter has no pytest installed the runner transparently falls
back to ``python -m unittest discover``. Both output formats are parsed.

FIX HISTORY
-----------
2026-08-23: NO_COLOR + elapsed fixes. Both subprocess calls now run with
NO_COLOR=1 in the child env - byte-stable, ANSI-free output for reliable parsing
(verified: this does NOT bypass _colorama_workaround's colorama import; that chain
only bites sandboxed python_exec children running pytest.main() directly).
`elapsed` is now computed right after execution so the early-return paths can
never reach `_parse_test_results` with it unbound; the rc==5 early-return block
(mis-indented relative to its parent) was reindented to match.

2026-10-06: path-artifact fix. Previously, when the configured working root had
no tests/, discovery fell through to a CWD/module-path walk-up that could land
on Jack's own repo (which DOES have tests/) - so with working_root set to
another project the runner "succeeded" against the wrong tree while the user's
project files were invisible ("its failure is a path artifact, not a code
issue"). Now: when a working root IS configured, discovery stays scoped to it
(working root itself -> its immediate parent -> its direct children) and fails
loudly instead of falling through. The CWD/module fallback applies ONLY when no
working root is configured at all (pure local dev / SERVER). An explicit
``project_root`` argument was added to target any project on demand.

2026-10-06: "NO TESTS RAN" fix. Jack's own test files are pytest-style plain
functions, which ``unittest discover`` does not collect (and in this venv it
silently found nothing) - the tool reported 0 tests while the suite existed.
pytest-first execution with a unittest fallback makes discovery reliable for
both styles; ``tests/__init__.py`` was also added so unittest-style packages
import cleanly everywhere.

2026-08-24: project-root resolution was hardcoded as three dirname() calls off
__file__. That only works when the module is imported from disk. When this tool
is delivered to the CLIENT over WS and exec'd by the dynamic loader, __file__ is
a sentinel ('<remote_tool_run_tests>') that abspath()'s against CWD (the CLIENT
dir), so the computed root pointed at a folder with no tests/ -> "Tests
directory not found" for BOTH the full run and a specific test_file.
_resolve_project_root() now locates the real project root from several anchors,
which works identically on disk (SERVER / local dev) and in the CLIENT sandbox.
"""

import subprocess
import sys
import os
import time
import re
from typing import Optional, Tuple

# Tool definition for auto-discovery
__tool_description__ = {
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": "Run the test suite to verify all tools are working correctly. "
                      "Targets the ACTIVE project: the tests/ folder inside the current "
                      "working root (or an explicit project_root argument). Per-project "
                      "suites live in <project>/tests - each project keeps its own suite "
                      "next to its code (pytest-style or unittest-style, both supported). "
                      "Use this after any code changes to ensure nothing is broken. Returns "
                      "detailed test results including pass/fail status, execution time, and "
                      "failure details.",
        "parameters": {
            "type": "object",
            "properties": {
                "test_file": {
                    "type": "string",
                    "description": "Optional: Run only a specific test file (e.g., 'test_python_exec'). "
                                 "If not provided, runs ALL tests."
                },
                "project_root": {
                    "type": "string",
                    "description": "Optional: Absolute path of the project to test. Must contain a "
                                 "tests/ folder with test files. Defaults to the current working root."
                },
                "verbose": {
                    "type": "boolean",
                    "description": "Whether to include detailed output for each test. Default: true.",
                    "default": True
                }
            },
            "required": []
        }
    }
}

# Marker files created by tests — cleaned up before and after runs
_TEST_MARKER_FILES = [
    "python_exec_test_marker.txt",
]

# Hard cap for a single test-suite run (seconds).
_TEST_TIMEOUT = 120


def _cleanup_test_artifacts():
    """Remove any leftover test marker files from previous runs."""
    # Use os.environ instead of importing tempfile (blocked by RemoteToolOrchestrator)
    temp_dir = os.environ.get('TEMP', os.path.expanduser('~'))
    for marker in _TEST_MARKER_FILES:
        path = os.path.join(temp_dir, marker)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass  # Ignore permission errors on cleanup


def _dir_has_tests(d: str) -> bool:
    """True when *d* is a project root containing a runnable tests/ folder.

    A valid marker is a 'tests' subdirectory holding at least one test_*.py file
    (the same pattern the runner discovers with). This avoids matching an unrelated
    folder that happens to be named 'tests'.
    """
    try:
        tests_dir = os.path.join(d, "tests")
        if not os.path.isdir(tests_dir):
            return False
        return any(f.startswith("test_") and f.endswith(".py")
                   for f in os.listdir(tests_dir))
    except OSError:
        return False


def _walk_up_for_tests(start: str, max_levels: int = 5) -> Optional[str]:
    """Walk up from *start* (inclusive) looking for a dir with tests/test_*.py."""
    d = start
    seen = set()
    for _ in range(max_levels + 1):
        try:
            if os.path.isdir(d) and d not in seen:
                seen.add(d)
                if _dir_has_tests(d):
                    return d
        except OSError:
            pass
        parent = os.path.dirname(d)
        if parent == d or not parent:
            break
        d = parent
    return None


def _working_root_anchor() -> Optional[str]:
    """Best-effort read of the system working root (the folder set in the CLIENT UI).

    Resolution order, first hit wins:
      a. injected ``get_working_root`` global (dynamic loader / sandbox),
      b. live import from tools.utils (on-disk SERVER process) - which itself resolves
         the per-turn ContextVar and falls back to the project root (2026-10-09).

    The old step c (direct read of CLIENT/config/.working_root.json) is gone: that file
    no longer exists; working roots are per-workspace values in the CLIENT database.

    Returns an absolute path when available, else None. Never raises - a missing
    working root must not break test discovery; the legacy anchors below still apply.
    """
    get_wr = globals().get("get_working_root")
    if callable(get_wr):
        try:
            wr = os.path.abspath(str(get_wr()))
            if os.path.isdir(wr):
                return wr
        except Exception:
            pass

    try:
        from tools.utils import get_working_root as _gwr  # type: ignore
        wr = os.path.abspath(str(_gwr()))
        if os.path.isdir(wr):
            return wr
    except Exception:
        pass

    return None


def _running_inside_jack_tree() -> bool:
    """True when this process is running from inside Jack's own code tree.

    Only used in the no-working-root fallback (step 2), where adopting the local
    tree is legitimate ONLY if the process really lives inside it - not when some
    unrelated CWD happens to sit under a folder that contains a tests/ directory.
    """
    try:
        if os.path.isfile(os.path.abspath(__file__)):
            return True  # imported from disk -> this IS the tree that ships us
    except Exception:
        pass
    cwd = os.getcwd()
    marker = os.path.join(cwd, "tools", "test_tools")
    if os.path.isdir(marker):
        return True
    parent_marker = os.path.join(os.path.dirname(cwd), "CLIENT", "tools", "test_tools")
    return os.path.isdir(parent_marker)


def _resolve_project_root(explicit: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Locate the project root whose tests/ suite should be run.

    Returns (root_or_None, explanation). ``root`` is None when no legitimate
    target exists - callers must then fail loudly with *explanation*.

    Resolution order:
      0. Explicit ``project_root`` argument (must exist; a missing or test-less
         explicit root fails loudly instead of silently re-discovering elsewhere).
      1. Configured working root (the active workspace's folder from the CLIENT DB) - checked as-is,
         then its immediate parent only (in case the project wraps the working
         folder), then among its direct children. Discovery NEVER climbs above
           that scope, so it cannot escape into an unrelated ancestor tree. This is
           what makes per-project suites work: a project that has tests/ inside
           (or around) the working root is tested exactly where it lives.
      2. NO working root configured at all (pure local dev / SERVER): legacy
         discovery - on-disk module path, then CWD walk-up for tests/test_*.py.

    IMPORTANT (2026-10-06 fix): before this change, a test-less working root fell
    through to the step-2 anchors and could land on Jack's own repo (which ships
    a tests/ folder) - so with working_root set to another project the runner
    silently executed THIS project's suite while the user's project files were
    invisible ("path artifact, not a code issue"). Failing loudly is always
    better than testing the wrong tree.
    """
    # 0) Explicit target wins outright - but never silently re-discover elsewhere.
    if explicit:
        cand = os.path.abspath(os.path.expanduser(explicit.strip()))
        if not os.path.isdir(cand):
            return None, (f"explicit project_root does not exist or is not a directory: {cand}")
        if _dir_has_tests(cand):
            return cand, "explicit project_root argument"
        return None, (f"explicit test-less project has no tests/ folder with test_*.py files: "
                      f"{os.path.join(cand, 'tests')}")

    # 1) Working-root anchor: test the live tree inside the configured working root.
    wr = _working_root_anchor()
    if wr and os.path.isdir(wr):
        if _dir_has_tests(wr):
            return wr, "configured working root (contains tests/)"

        # A project may wrap the working folder - but ONLY wr's IMMEDIATE parent is
        # checked (max_levels=0). Checking anything higher could climb out of the
        # user's tree into an unrelated ancestor that happens to ship its own tests/
        # (e.g. this repo) - the original path artifact.
        hit = _walk_up_for_tests(os.path.dirname(wr), max_levels=0)
        if hit:
            return hit, "project wrapping the configured working root"

        # A direct child of the working root may itself be a project.
        try:
            for name in sorted(os.listdir(wr)):
                cand = os.path.join(wr, name)
                if os.path.isdir(cand) and _dir_has_tests(cand):
                    return cand, f"direct child of working root ({name})"
        except OSError:
            pass

        # FIX (2026-10-06): configured working root has no tests anywhere nearby.
        # Do NOT fall through to CWD/module discovery - that used to silently pick
        # up Jack's own suite when the sandbox CWD sat inside the CLIENT dir, so
        # another project was "tested" against this repo's files (path artifact).
        return None, (f"configured working root {wr} contains no tests/ folder with test_*.py "
                      f"files (checked the working root itself, its immediate parent and its direct "
                      f"children). To run per-project tests, add a 'tests' folder with test files "
                      f"(pytest-style or unittest-style) inside your project at {wr}, or pass "
                      f"project_root explicitly to target another project.")

    # 2) No working root configured at all -> legitimate local-dev / SERVER mode:
    #    legacy discovery is safe here because there is no OTHER project in play.
    try:
        real_file = os.path.abspath(__file__)
        if os.path.isfile(real_file):
            legacy_root = os.path.dirname(os.path.dirname(os.path.dirname(real_file)))
            if _dir_has_tests(legacy_root):
                return legacy_root, "on-disk module path (no working root configured)"
    except Exception:
        pass

    try:
        hit = _walk_up_for_tests(os.getcwd())
        if hit and _running_inside_jack_tree():
            return hit, "CWD walk-up inside the Jack tree (no working root configured)"
    except Exception:
        pass

    # Nothing legitimate found - fail loudly (callers build the final message).
    return None, ("could not locate a project with tests/ : no working root is configured and "
                  "neither the module path nor the CWD tree contains a runnable tests/test_*.py suite")


def _normalize_test_file(test_file: str) -> str:
    """Reduce a test_file argument to a bare file name (no directory portion)."""
    name = (test_file or "").strip().replace("\\", "/")
    # Drop any directory portion so 'tests/test_x.py' == 'test_x.py'.
    return os.path.basename(name)


def _build_pytest_cmd(python_exe: str, test_file: Optional[str]) -> list[str]:
    """pytest command (preferred runner - collects plain functions AND TestCase classes)."""
    cmd = [python_exe, "-m", "pytest"]
    if test_file:
        cmd.append(os.path.join("tests", test_file))
    else:
        cmd.append("tests")
    cmd.extend(["-v", "--no-header", "-p", "no:cacheprovider"])
    return cmd


def _build_unittest_cmd(python_exe: str, test_file: Optional[str]) -> list[str]:
    """unittest command (fallback when the target interpreter has no pytest)."""
    cmd = [python_exe, "-m", "unittest"]
    if test_file:
        stem = test_file[:-3] if test_file.endswith(".py") else test_file
        cmd.extend(["-v", f"tests.{stem}"])
    else:
        cmd.extend(["discover", "-s", "tests", "-p", "test_*.py", "-v"])
    return cmd


def _pytest_missing(result) -> bool:
        """True when the run failed because the interpreter itself has no pytest.

        Only an interpreter-level import failure qualifies (the first line of stderr
        is the ModuleNotFoundError). A test file that merely fails to ``import pytest``
        must NOT trigger the unittest fallback - that would hide a real collection error.
        """
        err = (result.stderr or "").strip()
        return bool(re.match(r"No module named ['\"]?pytest", err, re.IGNORECASE))


def _runner_kind(runner: str) -> str:
    """'pytest' or 'unittest' - the output FORMAT to parse (independent of why
    unittest was used; a fallback label still describes unittest output)."""
    return "pytest" if runner.startswith("pytest") else "unittest"


def run_tests(test_file: Optional[str] = None, project_root: Optional[str] = None, verbose: bool = True) -> str:
    """
    Run the test suite of the active project and return results.

    Args:
        test_file: Optional specific test file to run (with or without .py extension).
                  If None, runs all tests in the target project's tests/ folder.
        project_root: Optional absolute path of the project to test. Must contain a
                     tests/ folder with test files. Defaults to the configured working
                     root - so each project keeps its own suite inside itself.
        verbose: Whether to include detailed per-test output.

    Returns:
        String with test results summary including pass/fail count, execution time,
        and failure details. When no legitimate target exists, a [FAIL] message
        explaining exactly what is missing (never silently runs the wrong project's
        suite).
    """
    root, how = _resolve_project_root(project_root)
    if not root:
        return f"[FAIL] No test target resolved - {how}"

    # Validate a specific file argument early for a clean error message.
    if test_file:
        norm = _normalize_test_file(test_file)
        if not norm or not (norm.endswith(".py") and norm.startswith("test_")):
            return "[FAIL] Invalid test_file argument - expected a name like 'test_python_exec.py'."
        if not os.path.isfile(os.path.join(root, "tests", norm)):
            return f"[FAIL] Test file not found in target project: {os.path.join(root, 'tests', norm)}"
        test_file = norm

    # Clean up any leftover artifacts from previous runs
    _cleanup_test_artifacts()

    python_exe = sys.executable if sys.executable else "python"
    start_time = time.time()
    try:
        # Preferred runner: pytest (collects both plain functions and TestCase).
        # NO_COLOR=1 makes pytest skip ANSI color codes -> byte-stable output for
        # parsing/logging. NOTE (verified against installed pytest 9.0.3): this does
        # NOT stop _pytest.capture._colorama_workaround() from importing colorama -
        # that function checks no env var at all; it only matters inside sandboxed
        # python_exec children (where ctypes is denied), which this subprocess path
        # never enters. In-sandbox pytest.main() runs need "-p no:capture" or a
        # pre-stubbed sys.modules["colorama"] instead.
        result = subprocess.run(
            _build_pytest_cmd(python_exe, test_file),
            cwd=root, capture_output=True, text=True, timeout=_TEST_TIMEOUT,
            env={**os.environ, "NO_COLOR": "1"}
        )
        runner = "pytest"

        # Fallback: the target interpreter has no pytest -> classic unittest.
        if _pytest_missing(result):
            runner = "unittest (fallback - no pytest in target interpreter)"
            result = subprocess.run(
                _build_unittest_cmd(python_exe, test_file),
                cwd=root, capture_output=True, text=True, timeout=_TEST_TIMEOUT,
                env={**os.environ, "NO_COLOR": "1"}
            )

        # Elapsed is computed immediately after execution so it is ALWAYS bound,
        # even when the early returns below exit before any later assignment.
        elapsed = time.time() - start_time

        # Hard-fail when pytest collected NOTHING: rc=5 with an empty summary would
        # otherwise be reported as 0 tests / [PASS]-adjacent noise. A suite that ran
        # zero tests is not a green light (mirrors the NO TESTS RAN warning for unittest).
        if runner.startswith("pytest") and result.returncode == 5:
            return ("[FAIL] No tests were collected - pytest exited with code 5 "
                    f"(no test_*.py files found under {os.path.join(root, 'tests')}, or none of them "
                    "define collectable tests). Add test functions/classes to the project's tests/ folder.")

        # Combine output for parsing
        full_output = result.stdout + result.stderr

        # Parse the output with robust regex (format-aware)
        summary = _parse_test_results(full_output, elapsed, runner)

        # Build the response
        response_parts = [
            "## Test Results",
            "",
            f"**Project root:** {root}",
            f"**Resolved via:** {how}",
            f"**Runner:** {runner}",
            "",
            summary
        ]

        # Add failure details if any tests failed or errored
        has_failures = result.returncode != 0
        if has_failures:
            response_parts.append("")
            response_parts.append("### Failure Details:")
            response_parts.append("```")
            failure_blocks = _extract_failure_details(full_output, runner)
            if failure_blocks:
                response_parts.extend(failure_blocks)
            else:
                # No structured blocks (pytest style) - show the tail of the output.
                response_parts.extend(full_output.strip().splitlines()[-40:])
            response_parts.append("```")

        # Add full verbose output if requested
        if verbose:
            lines = full_output.split('\n')
            response_parts.append("")
            response_parts.append("### Full Output:")
            response_parts.append("```")
            response_parts.extend(lines[-30:])  # Last 30 lines
            response_parts.append("```")

        return "\n".join(response_parts)

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        return (
            f"[FAIL] Tests timed out after {_TEST_TIMEOUT} seconds (ran for {elapsed:.1f}s). "
            f"A test may be hanging — check for infinite loops or blocking operations."
        )
    except Exception as e:
        return f"[FAIL] Failed to run tests: {str(e)}"
    finally:
        # Always clean up artifacts after runs
        _cleanup_test_artifacts()


def _extract_failure_details(output: str, runner: str) -> list[str]:
    """Extract individual test failure/error blocks from unittest output.

    Returns [] for pytest-style output (the caller then falls back to the tail).
    """
    if _runner_kind(runner) == "pytest":
        return []

    lines = output.split('\n')
    blocks = []
    current_block = None

    for line in lines:
        # Detect start of a failure or error block
        if re.match(r'^(FAIL|ERROR):', line.strip()):
            if current_block:
                blocks.append(current_block)
            current_block = [line]
        elif current_block is not None:
            # Collect lines until we hit the next test or summary
            if re.match(r'^(OK|FAILED|Ran |\d+ tests?)', line.strip()):
                blocks.append(current_block)
                current_block = None
            else:
                current_block.append(line)

    if current_block:
        blocks.append(current_block)

    # Return flattened with separators
    result = []
    for block in blocks:
        result.extend(block)
        result.append("---")
    return result


def _parse_test_results(output: str, elapsed: float, runner: str) -> str:
    """Parse test output into a summary (format-aware for pytest and unittest)."""
    total_tests = 0
    failed = 0
    errors = 0
    skipped = 0

    if _runner_kind(runner) == "pytest":
        # Final summary line, e.g. "= 34 passed, 2 failed, 1 error, 1 skipped in 0.5s ="
        m_pass = re.search(r'(\d+)\s+passed', output)
        m_fail = re.search(r'(\d+)\s+failed', output)
        m_err = re.search(r'(\d+)\s+errors?', output)
        m_skip = re.search(r'(\d+)\s+skipped', output)
        if m_pass:
            total_tests += int(m_pass.group(1))
        if m_fail:
            failed = int(m_fail.group(1))
        if m_err:
            errors = int(m_err.group(1))
        if m_skip:
            skipped = int(m_skip.group(1))
        total_tests += failed + errors + skipped
    else:
        # Match "Ran X tests in Y.YYYs"
        ran_match = re.search(r'Ran\s+(\d+)\s+tests?\s+in\s+[\d.]+s', output)
        if ran_match:
            total_tests = int(ran_match.group(1))

        # Match "OK" line (all passed, possibly with skips)
        ok_match = re.search(r'^OK\s*\((?:skips=\d+)?\)?$', output, re.MULTILINE)
        if ok_match:
            failed = 0
            errors = 0

        # Match "FAILED (...)" summary line for counts
        fail_match = re.search(r'FAILED\s*\(.*?\)', output)
        if fail_match:
            fail_str = fail_match.group(0)
            f_match = re.search(r'failures=(\d+)', fail_str)
            e_match = re.search(r'errors=(\d+)', fail_str)
            s_match = re.search(r'skipped=(\d+)', fail_str)

            if f_match:
                failed = int(f_match.group(1))
            if e_match:
                errors = int(e_match.group(1))
            if s_match:
                skipped = int(s_match.group(1))

    # Calculate passed
    passed = total_tests - failed - errors - skipped
    if passed < 0:
        passed = 0

    # Build summary
    status = "[PASS]" if (failed == 0 and errors == 0 and total_tests > 0) else "[FAIL]"
    no_tests_note = " (WARNING: NO TESTS RAN - the tests/ folder may not contain collectable test files)" \
        if total_tests == 0 else ""
    summary_lines = [
        f"**Status:** {status}{no_tests_note}",
        f"**Total Tests:** {total_tests}",
        f"**Passed:** {passed}",
        f"**Failed:** {failed}",
        f"**Errors:** {errors}",
        f"**Skipped:** {skipped}",
        f"**Execution Time:** {elapsed:.2f}s",
        ""
    ]

    return "\n".join(summary_lines)
