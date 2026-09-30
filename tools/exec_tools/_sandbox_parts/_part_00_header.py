# _part_00_header.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# HEADER -- banner + stdlib aliases (_jps_os/_jps_sys)
# Legacy source: lines 228-231 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
# === JACK python_exec runtime sandbox (auto-prepended; do not edit) ===
import os as _jps_os, sys as _jps_sys


