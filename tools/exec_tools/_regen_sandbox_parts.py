"""Regenerate tools/exec_tools/_sandbox_parts/ from the embedded SANDBOX_BOOTSTRAP literal.

The _sandbox_parts/ folder is a PROVENANCE VIEW of the in-file literal in
_sandbox_bootstrap.py (one file per original stage). It is NOT read at import time --
the CLIENT execs that module's source in-memory where no repo folder exists, so any
disk dependency would kill python_exec on every client (2026-09-18 split bug, fixed
2026-09-23 by embedding the literal).

Run from anywhere:  python tools/exec_tools/_regen_sandbox_parts.py
It rewrites each part file as: provenance header + marker line + verbatim fragment.
Fragments come from _sandbox_bootstrap.split_into_fragments(), so they are byte-exact
by construction; only the headers (provenance text) may be edited here.

The test suite (tests/test_sandbox_provenance.py) pins parts == literal,
so a manual edit below the marker line is caught immediately.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARTS_DIR = os.path.join(_HERE, "_sandbox_parts")

# Provenance headers per part file (everything ABOVE the marker). Keep them accurate:
# they document which original stage each fragment holds. The fragment text itself is
# generated -- never hand-edit below the marker in a part file.
_HEADERS = {
    "_part_00_header.py": [
        "# _part_00_header.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# HEADER -- banner + stdlib aliases (_jps_os/_jps_sys)",
        "# Legacy source: lines 228-231 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        # Current literal: lines 2-5 of the embedded literal.
    ],
    "_part_01_core.py": [
        "# _part_01_core.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# CORE -- JACK_PYEXEC_ROOT fail-closed check, READ_ROOTS (incl. every sys.path entry), _norm LRU cache, _inside prefix test, _deny, _jps_log/_jps_log_exc, devnull/PATH-dir helpers; 'def _jps_setup():' starts here",
        "# Legacy source: lines 232-435 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        # Current literal: lines 6-304 of the embedded literal.
    ],
    "_part_02_exec_policy_data.py": [
        "# _part_02_exec_policy_data.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# STAGE 0 -- child-process EXEC POLICY data + helpers (denylists, tokenizers, _check_cmd, interpreter detection);",
        "# 2026-11-14 additions: _winapi_is_write, _is_consent_decision/_deny_decision_write (parent-owned consent",
        "# decisions), _jps_fail (fail-closed stages), _SHELL_SUFFIXES (renamed shells), _check_cmd_paths (argv paths),",
        "# _SPAWN_TOKEN (one-use token for raw CreateProcess with executable=None), _check_cwd (spawn directory must",
        "# resolve inside working_root -- the verified cwd=C:\\Windows leak)",
        "# 2026-11-15 additions: _DENY_PROGS gains the Windows system-abuse utilities (wsl, reg, bitsadmin, certutil,",
        "# mshta, net/net1, netsh, schtasks, taskkill, tasklist, sc, msiexec, rundll32) plus ssh/fish/slogin -- wsl alone",
        "# defeats the sandbox (Linux side, no audit hook); _SHELL_SUFFIXES drops the bare 'sh'/'ash' stems (they denied",
        "# legitimate programs ending in 'sh': finish/flash/push/wash, verified false positives) and those shells are now",
        "# exact basenames; _check_cmd_paths skips Windows SWITCHES ('/r', '/c:x', '/m') instead of reading them as",
        "# absolute path arguments (ntpath.isabs('/list') is True -- verified false positives on sort /r, findstr /c:x).",
        "# Legacy source: lines 436-539 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        "# Current literal: lines 305-622 of the embedded literal.",
    ],
    "_part_03_audit_hook.py": [
        "# _part_03_audit_hook.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# STAGE 1 -- sys.addaudithook: open/enumeration/mutation/import/Popen events (primary runtime enforcement);",
        "# 2026-11-14 additions: flag-aware write detection on the open event, decision-file lock, audit-level",
        "# _winapi.CreateFile/_winapi.CreateProcess/socket.connect-connect_ex-sendto/os.system/os.spawn branches,",
        "# _POLICED_EVENTS fail-closed on internal hook errors, addaudithook failure -> _jps_fail (exit 3),",
        "# _check_cwd on the Popen event (args[2] on both platforms), the CreateProcess event (args[2]) and the",
        "# os.system/os.spawn branches (those fire no Popen event, so they inherit OUR cwd)",
        "# Legacy source: lines 540-650 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        "# Current literal: lines 623-897 of the embedded literal.",
    ],
    "_part_04_os_patches.py": [
        "# _part_04_os_patches.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# STAGE 2 -- os primitive patches + METADATA SCOPING (stat/lstat/access/readlink/os.path/pathlib) + flag-aware os.open",
        "# Legacy source: lines 651-913 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        # Current literal: lines 898-1190 of the embedded literal.
    ],
    "_part_05_subprocess_policy.py": [
        "# _part_05_subprocess_policy.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# STAGE 3 -- child-process EXEC POLICY patches: Popen.__init__, os.system/popen/spawn*/exec*/fork (second net over the hook)",
        "# 2026-11-14 addition: _check_cwd inside Popen.__init__ (keyword cwd= or positional slot) -- the wrapper sees",
        "# the caller's own cwd, which the audit event cannot show for a closure-extracted original __init__.",
        "# Legacy source: lines 914-1053 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        # Current literal: lines 1191-1342 of the embedded literal.
    ],
    "_part_06_shutil.py": [
        "# _part_06_shutil.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# STAGE 4 -- shutil high-level guards (copy/copy2/move/copytree/rmtree/archive writers)",
        "# Legacy source: lines 1054-1164 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        # Current literal: lines 1343-1455 of the embedded literal.
    ],
    "_part_07_archives.py": [
        "# _part_07_archives.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# STAGE 5 -- archive + compressed-file writers (zipfile/tarfile member traversal, gzip/bz2/lzma mode-aware open);",
        "# 2026-11-14: tarfile extractall validates self.getmembers() when members is None (was validating nothing)",
        "# and rejects filter='fully_trusted'; every mod-level failure now calls _jps_fail (exit 3) instead of",
        "# swallowing and continuing unsandboxed.",
        "# Legacy source: lines 1165-1301 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        "# Current literal: lines 1456-1604 of the embedded literal.",
    ],
    "_part_08_win32_misc.py": [
        "# _part_08_win32_misc.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# STAGE 6 -- raw Win32 (_winapi/nt) file primitives + builtins.open containment + socket outbound policy (loopback-only default; OUT-OF-SANDBOX NETWORK CONSENT GATE asking the local user via .temp/.jack_netreq request/decision files, 2026-11-24; admin open network via JACK_PYEXEC_NETWORK=1, 2026-09-30) + importlib/sqlite3 guards; last line ends _jps_setup()",
        "# 2026-11-14 additions: nt.listdir/scandir/_path_exists/_path_isdir/_path_isfile/_getfinalpathname/_getfullpathname",
        "# patched (raw enumeration + predicate leaks had no audit event), _winapi.CreateFile wrapper on access mask AND",
        "# creation disposition, _winapi.CreateProcess wrapper (command-line program + argv paths), decision-file lock",
        "# on every write route, narrowed device namespace (\\\\.\\pipe\\* + console names only), _jps_fail on every stage/mod",
        "# failure (no fail-open), _check_cwd in the _winapi.CreateProcess wrapper (positional slot 7 / keyword).",
        "# Legacy source: lines 1302-1663 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        "# Current literal: lines 1605-2274 of the embedded literal.",
    ],
    "_part_09_footer.py": [
        "# _part_09_footer.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP",
        "#",
        "# FOOTER -- module-level tail of the child payload: _jps_setup() call + B9 log (JACK_PYEXEC_DEBUG) + end marker",
        "# Legacy source: lines 1664-1674 of the pre-split single-file SANDBOX_BOOTSTRAP string.",
        # Current literal: lines 2275-2286 of the embedded literal.
    ],
}

_COMMON_FOOTER = [
    "# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):",
    "# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO",
    "# disk reads at import time -- the CLIENT execs that source in-memory where no repo",
    "# folder exists, so a runtime dependency on this folder would kill python_exec on every",
    "# client. This file documents ONE stage of the literal for auditability; regenerate it",
    "# with:  python tools/exec_tools/_regen_sandbox_parts.py",
    "# Keep everything below the marker VERBATIM (no new imports, no indentation changes).",
]

_MARKER = "# === FRAGMENT START (verbatim) ==="


def main():
    # Load the module from disk WITHOUT importing it as a package member: exec its source.
    sb_path = os.path.join(_HERE, "_sandbox_bootstrap.py")
    with open(sb_path, "r", encoding="utf-8") as fh:
        mod_src = fh.read()
    ns = {"__name__": "_sb_regen", "__file__": sb_path}
    exec(compile(mod_src, sb_path, "exec"), ns)

    fragments = dict(ns["split_into_fragments"]())
    expected_files = list(_HEADERS.keys())
    if sorted(fragments.keys()) != sorted(expected_files):
        raise SystemExit("part file set mismatch: %r vs %r" % (sorted(fragments), sorted(expected_files)))

    for fname in expected_files:
        header_lines = _HEADERS[fname] + _COMMON_FOOTER
        text = "\n".join(header_lines) + "\n" + _MARKER + "\n" + fragments[fname]
        path = os.path.join(_PARTS_DIR, fname)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("regenerated %-32s (%d chars)" % (fname, len(fragments[fname])))

    # Round-trip proof: re-read every file and confirm the join reproduces the literal.
    chunks = []
    for fname in expected_files:
        with open(os.path.join(_PARTS_DIR, fname), "r", encoding="utf-8", newline="") as fh:
            c = fh.read().replace("\r\n", "\n")
        assert _MARKER + "\n" in c, ("marker lost", fname)
        chunks.append(c.split(_MARKER + "\n", 1)[1])
    joined = "\n" + "".join(chunks)
    if joined != ns["SANDBOX_BOOTSTRAP"]:
        raise SystemExit("ROUND-TRIP MISMATCH -- aborting, parts/ NOT consistent with the literal")
    print("round-trip OK: parts join == SANDBOX_BOOTSTRAP (%d chars)" % len(joined))


if __name__ == "__main__":
    sys.exit(main())
