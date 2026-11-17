"""Regression tests for the CLIENT tool-source transform (parenthesized imports).

The CLIENT rewrites delivered tool source before exec(): imports become assignments
against injected symbols. A parenthesized list ('from tools.utils import (A,\\n B)') was
matched only on its OPENING line, so the rewrite emitted '(A = utils.(A' - a SyntaxError
that broke read_file/write_file/edit_file and every other tool carrying that form.

The CLIENT modules are imported inside a CHILD interpreter with cwd=CLIENT/ - exactly how
code_client.py runs them. In-process imports would collide with the SERVER root's own
'tools' package, which pytest puts on sys.path first.

Run:  venv/Scripts/python.exe -m pytest tools/test_tools/test_source_transform_paren_imports.py -v
"""

import json
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_CLIENT_ROOT = os.path.join(_REPO_ROOT, "CLIENT")

_CHILD = r"""
import json, os, sys

sys.path.insert(0, os.getcwd())          # cwd = CLIENT root, like code_client.py
from tools.remote_tools.loader.source_transform import transform_tool_source

cases = json.loads(sys.stdin.read())
out = {}
for label, src in cases.items():
    transformed = transform_tool_source(src, label)
    try:
        compile(transformed, "<transformed>", "exec")
        out[label] = {"ok": True, "source": transformed}
    except SyntaxError as e:
        out[label] = {"ok": False, "error": str(e), "source": transformed}
print("@@JSON@@" + json.dumps(out))
"""


def _run(cases: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _child_script()], cwd=_CLIENT_ROOT,
        input=json.dumps(cases), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"child interpreter failed: {proc.stderr[-1500:]}")
    marker = "@@JSON@@"
    payload = proc.stdout[proc.stdout.index(marker) + len(marker):]
    return json.loads(payload)


def _child_script() -> str:
    return _CHILD


class ParenImportTransformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = _run({
            "abs_inline_paren":
                "from tools.utils import (get_working_root,\n"
                "                     resolve_path_to_dir)\n"
                "def read_file():\n"
                "    return get_working_root, resolve_path_to_dir\n",
            "rel_inline_paren_alias":
                "from ..utils import (guard_path as gp,\n"
                "                      norm_name)\n"
                "def f():\n"
                "    return gp, norm_name\n",
            "own_line_list":
                "from ..utils import (\n"
                "    MODE_LF,\n"
                "    normalize_line_endings as norm,\n"
                ")\n"
                "def f():\n"
                "    return MODE_LF, norm\n",
            "same_pkg_paren":
                "from .profile_loader import (  # noqa\n"
                "    load_profile,\n"
                ")\n"
                "def f():\n"
                "    return load_profile\n",
            "config_paren":
                "from config import (\n"
                "    MODEL_NAME,\n"
                "    HOST,\n"
                ")\n"
                "def f():\n"
                "    return MODEL_NAME\n",
            "comment_in_list":
                "from tools.ssrf_guard import (  # shared policy\n"
                "    classify_blocked_url,\n"
                ")\n"
                "def f():\n"
                "    return classify_blocked_url\n",
            # A trailing comment that itself CONTAINS ')' ('# noqa: F401,E501 (re-exported)')
            # is the reason the closing paren must be searched in the CODE, not the raw line:
            # scanning the raw line stops the flattener one line early and leaves the rest of
            # the name list as a stray indented line (SyntaxError).
            "comment_with_paren_abs":
                "from tools.utils import (get_working_root,  # note (see docs)\n"
                "                         resolve_path_to_dir)\n"
                "def f():\n"
                "    return get_working_root, resolve_path_to_dir\n",
            "comment_with_paren_rel_alias":
                "from ..utils import (A as B,  # noqa (kept)\n"
                "                     C)\n"
                "def f():\n"
                "    return B, C\n",
            "comment_with_paren_config":
                "from config import (  # noqa: F401,E501  (re-exported)\n"
                "    MODEL_NAME,\n"
                "    HOST,\n"
                ")\n"
                "def f():\n"
                "    return MODEL_NAME\n",
            "docstring_shows_form":
                '"""Docs.\n\n    from ..utils import (\n        A,\n    )\n"""\n'
                "from ..utils import B\n"
                "def f():\n"
                "    return B\n",
            "indented_in_body":
                "def f():\n"
                "    from ..utils import (A,\n"
                "                          B as C)\n"
                "    return A, C\n",
            # A block whose ONLY statement is a docstring: masking must put the text back as
            # a real line, or the func/class/try body ends up empty (SyntaxError).
            "docstring_only_func":
                'def f():\n    """Only a docstring."""\n',
            "docstring_only_class":
                'class C:\n    """Only a docstring."""\n',
            "docstring_only_in_try":
                'def h():\n    try:\n        """note"""\n    except Exception:\n        pass\n',
            "docstring_only_module":
                '"""Just the module doc."""\n',
        })

    def _ok(self, label: str) -> str:
        entry = self.results[label]
        self.assertTrue(entry["ok"], f"{label}: {entry.get('error')}\n{entry['source']}")
        return entry["source"]

    def test_absolute_tools_import_with_inline_paren(self):
        """read_file's exact form: names start on the '(' line."""
        src = self._ok("abs_inline_paren")
        self.assertIn("get_working_root = utils.get_working_root", src)
        self.assertIn("resolve_path_to_dir = utils.resolve_path_to_dir", src)

    def test_relative_import_with_inline_paren_and_alias(self):
        src = self._ok("rel_inline_paren_alias")
        self.assertIn("gp = utils.guard_path", src)
        self.assertIn("norm_name = utils.norm_name", src)

    def test_list_opening_on_its_own_line(self):
        src = self._ok("own_line_list")
        self.assertIn("MODE_LF = utils.MODE_LF", src)
        self.assertIn("norm = utils.normalize_line_endings", src)

    def test_same_package_paren_import_is_neutralized(self):
        src = self._ok("same_pkg_paren")
        self.assertIn("pass", src)

    def test_config_paren_import_is_neutralized(self):
        src = self._ok("config_paren")
        self.assertIn("Removed config import", src)

    def test_comment_inside_name_list_is_dropped(self):
        src = self._ok("comment_in_list")
        self.assertIn("classify_blocked_url = ssrf_guard.classify_blocked_url", src)

    def test_comment_containing_a_paren_does_not_end_the_list(self):
        """'# noqa (re-exported)' must not be read as the closing paren of the import."""
        src = self._ok("comment_with_paren_abs")
        self.assertIn("get_working_root = utils.get_working_root", src)
        self.assertIn("resolve_path_to_dir = utils.resolve_path_to_dir", src)
        self.assertNotIn("resolve_path_to_dir)", src)

        src = self._ok("comment_with_paren_rel_alias")
        self.assertIn("B = utils.A", src)
        self.assertIn("C = utils.C", src)

        src = self._ok("comment_with_paren_config")
        self.assertIn("Removed config import", src)
        self.assertNotIn("HOST,", src)

    def test_docstring_showing_the_form_is_untouched(self):
        src = self._ok("docstring_shows_form")
        self.assertIn("    from ..utils import (\n        A,\n    )", src)
        self.assertIn("B = utils.B", src)

    def test_indented_import_inside_function_body(self):
        src = self._ok("indented_in_body")
        self.assertIn("    A = utils.A", src)
        self.assertIn("    C = utils.B", src)

    def test_docstring_only_blocks_survive_masking(self):
        """A block whose ONLY statement is a docstring must not become an empty body."""
        for label in ("docstring_only_func", "docstring_only_class",
                      "docstring_only_in_try", "docstring_only_module"):
            src = self._ok(label)
            self.assertIn('"""', src, f"{label}: docstring text must be restored")

    def test_real_delivered_tools_still_compile(self):
        """Every importable tool module in the SERVER repo survives transform + compile."""
        script = (
            "import os, sys\n"
            "sys.path.insert(0, os.getcwd())\n"
            "from tools.remote_tools.loader.source_transform import transform_tool_source\n"
            "root = r'" + _REPO_ROOT + "'\n"
            "checked, bad = 0, []\n"
            "for dirpath, dirs, files in os.walk(os.path.join(root, 'tools')):\n"
            "    dirs[:] = [d for d in dirs if d != '__pycache__']\n"
            "    for name in files:\n"
            "        if not name.endswith('.py'):\n"
            "            continue\n"
            "        with open(os.path.join(dirpath, name), encoding='utf-8') as fh:\n"
            "            src = fh.read()\n"
            "        try:\n"
            "            compile(src, name, 'exec')\n"
            "        except SyntaxError:\n"
            "            continue  # provenance fragments are indented text, not modules\n"
            "        out = transform_tool_source(src, name[:-3])\n"
            "        try:\n"
            "            compile(out, '<t>', 'exec')\n"
            "        except SyntaxError as e:\n"
            "            bad.append(name + ': ' + str(e))\n"
            "        checked += 1\n"
            "print('@@JSON@@' + __import__('json').dumps({'checked': checked, 'bad': bad}))\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], cwd=_CLIENT_ROOT,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        payload = proc.stdout[proc.stdout.index("@@JSON@@") + len("@@JSON@@"):]
        data = json.loads(payload)
        self.assertGreater(data["checked"], 40)
        self.assertEqual(data["bad"], [])


if __name__ == "__main__":
    unittest.main()
