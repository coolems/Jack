"""Regression tests for python_exec and the file line-ending policy (2026-10-23).

Run with:
    venv\\Scripts\\python.exe -m unittest tools.test_tools.test_python_exec_regressions -v

These tests exercise the REAL tool (a real child process per call), so they catch
the exact failure classes that were reported: decorators, future imports, wrong
traceback line numbers, dropped stderr, line-ending corruption and misleading
error messages.
"""

import os
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import tools.utils as utils
from tools.exec_tools import python_exec as pe
from tools.utils import normalize_line_endings
from tools.file_tools.write_file import write_file

_MISSING = object()


class PythonExecRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = os.path.join(_REPO, ".temp", "test_python_exec_regressions")
        os.makedirs(cls.workdir, exist_ok=True)
        cls._old_working_root = utils.get_working_root()
        utils.set_working_root(cls.workdir)
        cls._had_blocked = "PYTHON_EXEC_BLOCKED_LIBS" in pe.__dict__
        cls._old_blocked = pe.__dict__.get("PYTHON_EXEC_BLOCKED_LIBS")
        os.environ.pop("JACK_PYEXEC_DEBUG", None)

    @classmethod
    def tearDownClass(cls):
        utils.set_working_root(cls._old_working_root)
        if cls._had_blocked:
            pe.PYTHON_EXEC_BLOCKED_LIBS = cls._old_blocked
        else:
            pe.__dict__.pop("PYTHON_EXEC_BLOCKED_LIBS", None)

    def setUp(self):
        # Admin clean set = skip static guardrails; the runtime sandbox still runs.
        pe.PYTHON_EXEC_BLOCKED_LIBS = []

    def _with_default_blocklist(self):
        """Temporarily remove the injected admin set so DEFAULT_FORBIDDEN_MODULES applies."""
        old = pe.__dict__.pop("PYTHON_EXEC_BLOCKED_LIBS", _MISSING)
        return old

    def _restore_blocklist(self, old):
        if old is _MISSING:
            pe.__dict__.pop("PYTHON_EXEC_BLOCKED_LIBS", None)
        else:
            pe.PYTHON_EXEC_BLOCKED_LIBS = old

    def test_decorated_function_runs(self):
        result = pe.python_exec(
            "def deco(f):\n"
            "    return f\n"
            "\n"
            "@deco\n"
            "def f():\n"
            "    return 7\n"
            "print(f())\n"
        )
        self.assertEqual(result.strip(), "7")

    def test_decorated_class_runs(self):
        result = pe.python_exec(
            "def deco(cls):\n"
            "    cls.tag = 'decorated'\n"
            "    return cls\n"
            "\n"
            "@deco\n"
            "class C:\n"
            "    pass\n"
            "print(C.tag)\n"
        )
        self.assertEqual(result.strip(), "decorated")

    def test_from_future_import_runs(self):
        result = pe.python_exec(
            "from __future__ import annotations\n"
            "def f(x: int) -> int:\n"
            "    return x + 1\n"
            "print(f(1))\n"
        )
        self.assertEqual(result.strip(), "2")

    def test_traceback_uses_user_line_numbers(self):
        result = pe.python_exec("a = 1\nb = 2\nraise ValueError('boom')\n")
        self.assertIn("Your code line 3", result)
        self.assertIn('File "<python_exec>", line 3', result)
        self.assertNotIn("<python_exec_payload>", result)
        self.assertIn("ValueError: boom", result)

    def test_syntax_error_uses_user_line_numbers(self):
        result = pe.python_exec("a = 1\nb = (\n")
        self.assertIn("syntax error at line 2", result)
        self.assertIn('File "<python_exec>", line 2', result)
        self.assertNotIn("<python_exec_payload>", result)

    def test_stderr_is_surfaced_on_success(self):
        result = pe.python_exec(
            "import sys\n"
            "print('out')\n"
            "print('diagnostic-warning', file=sys.stderr)\n"
        )
        self.assertIn("out", result)
        self.assertIn("diagnostic-warning", result)
        self.assertIn("stderr", result)

    def test_output_preserves_leading_whitespace(self):
        result = pe.python_exec("print('  indented output')\n")
        self.assertEqual(result, "  indented output")

    def test_argv0_is_stable(self):
        result = pe.python_exec("import sys\nprint(sys.argv)\n")
        self.assertIn("<python_exec>", result)

    def test_dunder_file_is_defined(self):
        result = pe.python_exec("print(__file__)\n")
        self.assertIn("<python_exec>", result)

    def test_python_exec_writes_lf_not_crlf(self):
        path = os.path.join(self.workdir, "write_lf.txt")
        result = pe.python_exec(
            "p = r'%s'\n"
            "open(p, 'w', encoding='utf-8').write('a\\nb\\nc\\n')\n"
            "print('ok')\n" % path
        )
        self.assertIn("ok", result)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"a\nb\nc\n")

    def test_read_modify_write_keeps_lf(self):
        path = os.path.join(self.workdir, "rmw_lf.txt")
        with open(path, "wb") as fh:
            fh.write(b"a\nb\nc\n")
        result = pe.python_exec(
            "p = r'%s'\n"
            "s = open(p, encoding='utf-8').read()\n"
            "s = s.replace('b', 'B')\n"
            "open(p, 'w', encoding='utf-8').write(s)\n"
            "print('ok')\n" % path
        )
        self.assertIn("ok", result)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"a\nB\nc\n")

    def test_crlf_content_is_not_doubled(self):
        path = os.path.join(self.workdir, "crlf_preserved.txt")
        result = pe.python_exec(
            "p = r'%s'\n"
            "open(p, 'w', encoding='utf-8').write('a\\r\\nb\\r\\n')\n"
            "print('ok')\n" % path
        )
        self.assertIn("ok", result)
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertEqual(data, b"a\r\nb\r\n")
        self.assertNotIn(b"\r\r\n", data)

    def test_pathlib_write_text_has_no_double_cr(self):
        path = os.path.join(self.workdir, "pathlib_write.txt")
        result = pe.python_exec(
            "from pathlib import Path\n"
            "p = Path(r'%s')\n"
            "p.write_text('a\\r\\nb\\r\\n', encoding='utf-8')\n"
            "print('ok')\n" % path
        )
        self.assertIn("ok", result)
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertNotIn(b"\r\r\n", data)

    def test_long_traceback_keeps_exception_message(self):
        lines = ["def f0():\n    raise ValueError('unique-tail-marker')\n"]
        for i in range(1, 120):
            lines.append("def f%d():\n    return f%d()\n" % (i, i - 1))
        lines.append("f119()\n")
        result = pe.python_exec("".join(lines))
        self.assertIn("unique-tail-marker", result)

    def test_exit_3_is_not_reported_as_sandbox_init(self):
        result = pe.python_exec("import sys\nprint('before')\nsys.exit(3)\n")
        self.assertIn("exit(3)", result)
        self.assertNotIn("fail-closed init", result)
        self.assertIn("before", result)

    def test_timeout_keeps_partial_output(self):
        result = pe.python_exec(
            "import time\n"
            "print('started', flush=True)\n"
            "time.sleep(10)\n",
            timeout=1.0,
        )
        self.assertIn("EXECUTION TIMEOUT", result)
        self.assertIn("started", result)
        self.assertIn("partial stdout", result)

    def test_invalid_timeout_has_clear_message(self):
        result = pe.python_exec("print(1)\n", timeout="soon")
        self.assertIn("timeout must be a number", result)

    def test_path_scope_allows_route_strings(self):
        old = self._with_default_blocklist()
        try:
            result = pe.python_exec("print('/api/v1/users')\n")
        finally:
            self._restore_blocklist(old)
        self.assertIn("/api/v1/users", result)
        self.assertNotIn("EXECUTION BLOCKED", result)

    def test_path_scope_blocks_open_outside_root(self):
        old = self._with_default_blocklist()
        try:
            result = pe.python_exec("open('/etc/passwd').read()\n")
        finally:
            self._restore_blocklist(old)
        self.assertIn("EXECUTION BLOCKED by path-scope guardrail", result)
        self.assertIn("/etc/passwd", result)

    def test_write_file_normalizes_to_lf(self):
        msg = write_file(filename="normalized.txt", content="a\r\nb\r\r\nc", path="")
        self.assertIn("LF line endings", msg)
        with open(os.path.join(self.workdir, "normalized.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"a\nb\nc")

    def test_write_file_keeps_crlf_for_batch_files(self):
        msg = write_file(filename="run.bat", content="echo a\necho b\n", path="")
        self.assertIn("CRLF line endings", msg)
        with open(os.path.join(self.workdir, "run.bat"), "rb") as fh:
            self.assertEqual(fh.read(), b"echo a\r\necho b\r\n")

    def test_write_file_rejects_unknown_mode(self):
        msg = write_file(filename="x.txt", content="x", path="", mode="x")
        self.assertIn("mode must be 'w'", msg)

    def test_normalizer_repairs_double_cr(self):
        self.assertEqual(normalize_line_endings("a\r\r\nb", "x.py"), "a\nb")
        self.assertEqual(normalize_line_endings("a\r\nb\r\nc", "x.py"), "a\nb\nc")
        self.assertEqual(normalize_line_endings("a\nb", "x.bat"), "a\r\nb")


if __name__ == "__main__":
    unittest.main(verbosity=2)
