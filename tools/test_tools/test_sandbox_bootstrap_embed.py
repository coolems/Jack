"""Tests: _sandbox_bootstrap.py must be delivery-context-proof (2026-09-23 fix).

The 2026-09-18 split made the module read tools/exec_tools/_sandbox_parts/ at import
time. That killed python_exec on every CLIENT: the SERVER ships this file as a shared
dep that the client execs in-memory (no __file__, cwd = CLIENT dir / user working_root),
where no such folder exists -> FileNotFoundError -> required-dep gate -> tool dead.

The fix embeds SANDBOX_BOOTSTRAP as an in-file literal (zero disk I/O at import) and
keeps _sandbox_parts/ only as a provenance view, pinned here byte-for-byte.

Run:  python -m pytest tools/test_tools/test_sandbox_bootstrap_embed.py -v
"""

import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SB_PATH = os.path.join(_REPO_ROOT, "tools", "exec_tools", "_sandbox_bootstrap.py")
_PARTS_DIR = os.path.join(_REPO_ROOT, "tools", "exec_tools", "_sandbox_parts")

MARKER = "# === FRAGMENT START (verbatim) ==="


def _load_module_source():
    with open(_SB_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _read_part_fragment(fname):
    """Read one part file's verbatim fragment (everything below the marker)."""
    path = os.path.join(_PARTS_DIR, fname)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        c = fh.read().replace("\r\n", "\n")  # line-ending agnostic, like the module's own build
    assert MARKER + "\n" in c, ("marker missing in part file", fname)
    return c.split(MARKER + "\n", 1)[1]


def _exec_module_like_client():
    """Exec the module source exactly like the CLIENT loader does:

    * no __file__ (pure exec context), synthetic module identity under 'tools.*',
      cwd = a foreign directory with NO repo folders anywhere up the tree.
    Returns the resulting globals dict.
    """
    import tempfile
    foreign_cwd = os.path.join(tempfile.gettempdir(), "jack_sb_test_foreign")
    os.makedirs(foreign_cwd, exist_ok=True)
    cwd_before = os.getcwd()
    os.chdir(foreign_cwd)
    try:
        ns = {
            "__name__": "tools.exec_tools._sandbox_bootstrap",
            "__package__": "tools.exec_tools",
            "__builtins__": __builtins__,
        }
        exec(compile(_load_module_source(), "<remote_exec_tools._sandbox_bootstrap>", "exec"), ns)
        return ns
    finally:
        os.chdir(cwd_before)


def test_embedded_literal_compiles_and_has_markers():
    """The literal must be valid Python and carry the sandbox's start/end markers."""
    src = _load_module_source()
    assert "SANDBOX_BOOTSTRAP = r'''" in src, "in-file literal missing"
    ns = _exec_module_like_client()
    value = ns["SANDBOX_BOOTSTRAP"]
    compile(value, "<SANDBOX_BOOTSTRAP>", "exec")  # fail-closed check (module does this at import too)
    assert "# === JACK python_exec runtime sandbox" in value
    assert "# === end python_exec runtime sandbox ===" in value


def test_module_does_no_disk_reads_at_import():
    """The module source must not reference the parts folder as a RUNTIME dependency."""
    src = _load_module_source()
    # The old disk-read implementation is gone.
    assert "_build_sandbox_bootstrap_from_parts" not in src, "old import-time file reader still present"
    assert "_sandbox_parts_dir" not in src, "old folder locator still present"
    # No module-level os usage before the literal (the literal itself may mention _jps_os -- that is CHILD code).
    pre_literal = src.split("SANDBOX_BOOTSTRAP = r'''")[0]
    assert "\nimport os" not in pre_literal, "module imports os at top level again"


def test_parts_are_byte_identical_to_embedded_literal():
    """_sandbox_parts/ must be an exact provenance view of the literal (no silent drift)."""
    ns = _exec_module_like_client()
    fragments = dict(ns["split_into_fragments"]())
    expected_files = [
        "_part_00_header.py", "_part_01_core.py", "_part_02_exec_policy_data.py",
        "_part_03_audit_hook.py", "_part_04_os_patches.py", "_part_05_subprocess_policy.py",
        "_part_06_shutil.py", "_part_07_archives.py", "_part_08_win32_misc.py", "_part_09_footer.py",
    ]
    assert sorted(fragments.keys()) == sorted(expected_files), "part file set mismatch"
    for fname in expected_files:
        on_disk = _read_part_fragment(fname)
        assert on_disk == fragments[fname], (
            "provenance fragment out of sync with the embedded literal: %s -- "
            "run python tools/exec_tools/_regen_sandbox_parts.py" % fname
        )


def test_client_exec_context_yields_identical_value():
    """Exec WITHOUT __file__ from a foreign cwd must produce the same value as a normal import."""
    # Normal (server-style) load: real package import.
    sys.path.insert(0, _REPO_ROOT)
    try:
        import tools.exec_tools._sandbox_bootstrap as server_mod
        server_value = server_mod.SANDBOX_BOOTSTRAP
    finally:
        sys.path.remove(_REPO_ROOT)

    client_ns = _exec_module_like_client()
    assert client_ns["SANDBOX_BOOTSTRAP"] == server_value, (
        "CLIENT exec context produced a different SANDBOX_BOOTSTRAP than the server import"
    )


def test_old_disk_read_failure_mode_is_gone():
    """Regression: the exact Mac-client failure was FileNotFoundError from _sandbox_parts_dir().

    Simulate it directly -- even if someone re-introduces a folder lookup, this proves the
    current module never consults one (the client exec above already ran in a cwd tree with
    no tools/exec_tools/_sandbox_parts anywhere)."""
    import tempfile
    foreign_cwd = os.path.join(tempfile.gettempdir(), "jack_sb_test_foreign2")
    os.makedirs(foreign_cwd, exist_ok=True)
    cwd_before = os.getcwd()
    os.chdir(foreign_cwd)
    try:
        # No __file__ at all -- the strictest client context. If the module tried to locate
        # _sandbox_parts/ relative to anything on disk, this raises FileNotFoundError.
        ns = {"__name__": "tools.exec_tools._sandbox_bootstrap", "__package__": "tools.exec_tools"}
        exec(compile(_load_module_source(), "<remote>", "exec"), ns)
    finally:
        os.chdir(cwd_before)
    assert isinstance(ns["SANDBOX_BOOTSTRAP"], str) and len(ns["SANDBOX_BOOTSTRAP"]) > 10000


def test_synthetic_file_context_also_works():
    """The loader also sets a synthetic __file__ like '<remote_exec_tools._sandbox_bootstrap>'.

    A path-based lookup against that string must not be relied upon -- the value comes from
    the literal regardless."""
    import tempfile
    foreign_cwd = os.path.join(tempfile.gettempdir(), "jack_sb_test_foreign3")
    os.makedirs(foreign_cwd, exist_ok=True)
    cwd_before = os.getcwd()
    os.chdir(foreign_cwd)
    try:
        ns = {
            "__name__": "tools.exec_tools._sandbox_bootstrap",
            "__package__": "tools.exec_tools",
            "__file__": "<remote_exec_tools._sandbox_bootstrap>",  # synthetic, not a real path
        }
        exec(compile(_load_module_source(), "<remote2>", "exec"), ns)
    finally:
        os.chdir(cwd_before)
    assert isinstance(ns["SANDBOX_BOOTSTRAP"], str) and len(ns["SANDBOX_BOOTSTRAP"]) > 10000
