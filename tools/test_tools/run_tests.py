"""Run Tests Tool - Execute the test suite and report results.

This tool runs all tests in the tests/ directory and returns
a summary of results. Use this after any code changes to verify
nothing is broken.

Usage:
    run_tests() - Run all tests
    run_tests(test_file="test_python_exec") - Run specific test file

FIX (2026-08-24): project-root resolution was hardcoded as three dirname() calls
off __file__. That only works when the module is imported from disk. When this tool
is delivered to the CLIENT over WS and exec'd by the dynamic loader, __file__ is a
sentinel ('<remote_tool_run_tests>') that abspath()'s against CWD (the CLIENT dir),
so the computed root pointed at a folder with no tests/ -> "Tests directory not found"
for BOTH the full run and a specific test_file. _resolve_project_root() now locates
the real project root by searching for a tests/test_*.py marker from several anchors,
which works identically on disk (SERVER / local dev) and in the CLIENT sandbox.
"""

import subprocess
import sys
import os
import time
import re
import json
from typing import Optional

# Tool definition for auto-discovery
__tool_description__ = {
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": "Run the test suite to verify all tools are working correctly. "
                      "Use this after any code changes to ensure nothing is broken. "
                      "Returns detailed test results including pass/fail status, "
                      "execution time, and failure details.",
        "parameters": {
            "type": "object",
            "properties": {
                "test_file": {
                    "type": "string",
                    "description": "Optional: Run only a specific test file (e.g., 'test_python_exec'). "
                                 "If not provided, runs ALL tests."
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
    """True when *d* is a project root containing a runnable tests/ package.

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
      b. live import from tools.utils (on-disk SERVER process),
      c. direct read of CLIENT/config/.working_root.json (or <CWD>/config/ when this
         process runs with its CWD inside the CLIENT dir, as the client sandbox does).

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

    # FIX (2026-08-28): .working_root.json moved from the CLIENT root to the CLIENT
    # config folder -- the old root-level candidate is gone. When this process runs
    # with its CWD inside the CLIENT dir (the client sandbox chdirs there), the file
    # also lives at <CWD>/config/.
    candidates = []
    try:
        module_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(module_dir, "..", "..", "CLIENT", "config", ".working_root.json"))
    except Exception:
        pass
    try:
        candidates.append(os.path.join(os.getcwd(), "config", ".working_root.json"))
    except Exception:
        pass

    for cand in candidates:
        try:
            if not os.path.isfile(cand):
                continue
            with open(cand, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved = str((data or {}).get("working_root") or "").strip()
            if saved and os.path.isdir(saved):
                return os.path.abspath(saved)
        except Exception:
            continue
    return None


def _resolve_project_root() -> str:
    """Locate the project root that contains the tests/ suite.

    Resolution order (first hit wins):
      1. Working-root anchor - the system working root set in the CLIENT UI
         (.working_root.json). Checked as-is, then walked up from its parent,
         then among its direct children (e.g. base/local_ai). This makes the
         runner always test the LIVE tree inside the working root, not a stale
         copy of the code that happens to be imported from disk or run from CWD.
      2. On-disk module path - if __file__ is a real file, walk up three levels
         as before (SERVER / local dev where no working root is configured).
      3. CWD anchor - walk up from the current working directory for tests/test_*.py.

    Falls back to the legacy three-dirname computation so the original "Tests directory
    not found" message is still produced when nothing can be located (fail loudly).
    """
    # 1) Working-root anchor: test the live tree inside the configured working root.
    wr = _working_root_anchor()
    if wr and os.path.isdir(wr):
        # a) the working root itself is the project root
        if _dir_has_tests(wr):
            return wr
        # b) walk up from it (working root nested inside the project)
        hit = _walk_up_for_tests(os.path.dirname(wr))
        if hit:
            return hit
        # c) a direct child of working_root is the project (e.g. base/local_ai)
        try:
            for name in sorted(os.listdir(wr)):
                cand = os.path.join(wr, name)
                if os.path.isdir(cand) and _dir_has_tests(cand):
                    return cand
        except OSError:
            pass

    # 2) Real on-disk module -> classic layout: <root>/tools/test_tools/run_tests.py
    try:
        real_file = os.path.abspath(__file__)
        if os.path.isfile(real_file):
            legacy_root = os.path.dirname(os.path.dirname(os.path.dirname(real_file)))
            if _dir_has_tests(legacy_root):
                return legacy_root
    except Exception:
        pass

    # 3) CWD anchor (CLIENT sandbox runs with CWD inside the project tree).
    try:
        hit = _walk_up_for_tests(os.getcwd())
        if hit:
            return hit
    except Exception:
        pass

    # Fallback: legacy computation (keeps the original error path intact).
    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        return os.getcwd()


def _normalize_test_file(test_file: str) -> str:
    """Reduce a test_file argument to a bare module stem (no path, no .py)."""
    name = (test_file or "").strip().replace("\\", "/")
    # Drop any directory portion and the extension so 'tests/test_x.py' == 'test_x'.
    name = os.path.basename(name)
    if name.endswith(".py"):
        name = name[:-3]
    return name


def run_tests(test_file: Optional[str] = None, verbose: bool = True) -> str:
    """
    Run the test suite and return results.

    Args:
        test_file: Optional specific test file to run (with or without .py extension).
                  If None, runs all tests.
        verbose: Whether to include detailed per-test output.

    Returns:
        String with test results summary including pass/fail count,
        execution time, and failure details.
    """
    project_root = _resolve_project_root()
    tests_dir = os.path.join(project_root, "tests")

    # Check if tests directory exists
    if not os.path.exists(tests_dir):
        return (f"[FAIL] Tests directory not found at {tests_dir}. No tests to run. "
                f"(resolved project root: {project_root})")

    # Clean up any leftover artifacts from previous runs
    _cleanup_test_artifacts()

    # Build the command
    python_exe = sys.executable if sys.executable else "python"
    cmd = [python_exe, "-m", "unittest"]

    if test_file:
        # Run specific test file (normalized to a bare module stem)
        stem = _normalize_test_file(test_file)
        if not stem:
            return "[FAIL] Invalid test_file argument - nothing to run."
        test_module = f"tests.{stem}"
        cmd.extend(["-v", test_module])
    else:
        # Run all tests
        cmd.extend([
            "discover",
            "-s", "tests",
            "-p", "test_*.py",
            "-v"
        ])

    # Run the tests
    start_time = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=120  # 2 minute max for all tests
        )
        elapsed = time.time() - start_time

        # Combine output for parsing
        full_output = result.stdout + result.stderr

        # Parse the output with robust regex
        summary = _parse_test_results(full_output, elapsed)

        # Build the response
        response_parts = [
            "## Test Results",
            "",
            f"**Project root:** {project_root}",
            "",
            summary
        ]

        # Add failure details if any tests failed or errored
        has_failures = result.returncode != 0
        if has_failures:
            response_parts.append("")
            response_parts.append("### Failure Details:")
            response_parts.append("```")
            # Extract individual failure/error blocks
            failure_blocks = _extract_failure_details(full_output)
            if failure_blocks:
                response_parts.extend(failure_blocks)
            else:
                response_parts.append("(No detailed failure output available)")
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
            f"[FAIL] Tests timed out after 120 seconds (ran for {elapsed:.1f}s). "
            f"A test may be hanging — check for infinite loops or blocking operations."
        )
    except Exception as e:
        return f"[FAIL] Failed to run tests: {str(e)}"
    finally:
        # Always clean up artifacts after runs
        _cleanup_test_artifacts()


def _extract_failure_details(output: str) -> list[str]:
    """Extract individual test failure/error blocks from unittest output."""
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


def _parse_test_results(output: str, elapsed: float) -> str:
    """Parse test output into a summary using robust regex patterns."""
    total_tests = 0
    failed = 0
    errors = 0
    skipped = 0

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
    status = "[PASS]" if failed == 0 and errors == 0 else "[FAIL]"
    summary_lines = [
        f"**Status:** {status}",
        f"**Total Tests:** {total_tests}",
        f"**Passed:** {passed}",
        f"**Failed:** {failed}",
        f"**Errors:** {errors}",
        f"**Skipped:** {skipped}",
        f"**Execution Time:** {elapsed:.2f}s",
        ""
    ]

    return "\n".join(summary_lines)
