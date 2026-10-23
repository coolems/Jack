"""Regression tests for CLIENT/tools/remote_tools/loader/source_transform.py.

Bug fixed 2026-10-23 (log: 'Compilation failed for tool write_file: invalid syntax,
line 39'): a multi-line parenthesized multi-dot import in delivered tool source --

    from ..utils import (
        MAX_WRITE_SIZE,
        get_working_root,
    )

was rewritten line-by-line to '( = utils.(' by step 1's single-line regex, so the
transformed code could never compile on the CLIENT while working fine in the SERVER
workspace. The fix is _flatten_multi_dot_paren_imports() (collapse to one line before
the rewrite). These tests guard that path and its neighbours.
"""

import os
import sys


def _load_transform():
    """Import the transform module, tolerating both package layouts."""
    try:
        from tools.remote_tools.loader import source_transform
        return source_transform
    except ImportError:
        pass
    import importlib.util
    import types
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "..", "CLIENT", "tools", "remote_tools",
                        "loader", "source_transform.py")
    path = os.path.normpath(path)
    src = open(path, encoding="utf-8").read()
    mod = types.ModuleType("source_transform_under_test")
    exec(compile(src, path, "exec"), mod.__dict__)
    sys.modules["source_transform_under_test"] = mod
    return mod


st = _load_transform()

MULTI_LINE_PAREN_IMPORT = """\
import os

from ..utils import (
    MAX_WRITE_SIZE,
    get_working_root,
)

def write_file(filename):
    from ..utils import (
        resolve_path_to_dir,
        guard_path_inside_working_root as gpiwr,
    )
    return filename
"""


def test_multiline_paren_import_flattened():
    out = st.transform_tool_source(MULTI_LINE_PAREN_IMPORT, "write_file")
    assert "( = utils.(" not in out, "paren import must be flattened before rewrite"
    # module-level names become assignments against the injected 'utils' symbol
    assert "MAX_WRITE_SIZE = utils.MAX_WRITE_SIZE" in out
    assert "get_working_root = utils.get_working_root" in out


def test_multiline_paren_import_compiles():
    out = st.transform_tool_source(MULTI_LINE_PAREN_IMPORT, "write_file")
    compile(out, "<test>", "exec")  # must not raise SyntaxError


def test_function_body_paren_import_keeps_indentation():
    # the fixture's function body is indented 4 spaces -- that exact indentation
    # must survive the flatten + rewrite (the old bug collapsed it to column 0).
    out = st.transform_tool_source(MULTI_LINE_PAREN_IMPORT, "write_file")
    lines = [l for l in out.split("\n") if "resolve_path_to_dir" in l]
    assert lines and lines[0].startswith("    "), f"indentation lost: {lines!r}"
    # alias form inside the body is preserved
    assert any(l.strip() == "gpiwr = utils.guard_path_inside_working_root"
               for l in out.split("\n"))


def test_real_write_file_source_transforms_and_compiles():
    """The actual delivered tool that triggered the incident (2026-10-23)."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "file_tools", "write_file.py"))
    if not os.path.exists(path):
        return  # layout differs -- skip rather than fail
    src = open(path, encoding="utf-8").read()
    out = st.transform_tool_source(src, "write_file")
    compile(out, "<test_write_file>", "exec")


if __name__ == "__main__":
    for name in sorted(n for n in globals() if n.startswith("test_")):
        getattr(globals()[name])()
        print(f"PASS {name}")
    print("ALL PASS")
