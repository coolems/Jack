# _part_09_footer.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# FOOTER -- module-level tail of the child payload: _jps_setup() call + B9 log (JACK_PYEXEC_DEBUG) + end marker
# Legacy source: lines 1664-1674 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
_jps_setup()
try:
    import sys as _jps_sys3, os as _jps_os3
    if _jps_os3.environ.get("JACK_PYEXEC_DEBUG", "0") != "0":
        _ld = _jps_os3.path.join(_jps_os3.environ.get("JACK_PYEXEC_ROOT") or ".", ".temp")  # user policy (2026-09-18): child debug log lives in .temp/
        _jps_os3.makedirs(_ld, exist_ok=True)
        with open(_jps_os3.path.join(_ld, ".jack_pyexec_child.log"), "a", encoding="utf-8") as _lf3:
            _lf3.write("[PYEXEC] B9: sandbox bootstrap complete -- user code may now run" + chr(10))
except Exception:
    pass
# === end python_exec runtime sandbox ===
