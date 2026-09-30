# _sandbox_parts/ -- PROVENANCE fragments of SANDBOX_BOOTSTRAP (2026-09-18 split, 2026-09-23 re-scoped)

The `SANDBOX_BOOTSTRAP` value lives as an **in-file `r'''` literal** inside
`_sandbox_bootstrap.py`. This folder holds the same content split into **ten verbatim
text fragments**, one per original stage, purely as an auditable provenance view.

## Why a literal and not runtime file reads? (2026-09-23 fix)
The 2026-09-18 split made the parent module READ these files at import time. That broke
CLIENT delivery: the SERVER ships `_sandbox_bootstrap.py` as a shared dep that the CLIENT
execs **in-memory** (no `__file__`, cwd = the user's working_root), where no such folder
exists -> `FileNotFoundError` on every client, killing `python_exec`. The literal makes
the module delivery-context-proof: server import / CLIENT exec / standalone all produce
identical bytes with zero disk I/O at import time.

## Files (original line ranges of the pre-split string)
| file | lines | content |
|---|---|---|
| _part_00_header.py | 228-231 | banner + `import os as _jps_os, sys as _jps_sys` |
| _part_01_core.py | 232-435 | root/READ_ROOTS/_norm/_inside/_deny/logging helpers; `_jps_setup()` def starts here |
| _part_02_exec_policy_data.py | 436-539 | stage 0: exec-policy denylists + tokenizers + `_check_cmd` |
| _part_03_audit_hook.py | 540-650 | stage 1: `sys.addaudithook` enforcement |
| _part_04_os_patches.py | 651-913 | stage 2: os primitive patches + metadata scoping + flag-aware `os.open` |
| _part_05_subprocess_policy.py | 914-1053 | stage 3: Popen/spawn/exec/fork policy |
| _part_06_shutil.py | 1054-1164 | stage 4: shutil high-level guards |
| _part_07_archives.py | 1165-1301 | stage 5: zip/tar/gzip/bz2/lzma writers |
| _part_08_win32_misc.py | 1302-1663 | stage 6: raw Win32 + builtins.open + sockets + importlib/sqlite3 (end of `_jps_setup`) |
| _part_09_footer.py | 1664-1674 | module-level tail: `_jps_setup()` call + B9 log + end marker |

## Rules for editing a fragment
* Keep the provenance header lines above the `# === FRAGMENT START (verbatim) ===` marker.
* Never add `import` statements or change indentation of existing fragment lines below it.
* The test suite pins this folder to the literal byte-for-byte:
  `python -m pytest tools/test_tools/test_sandbox_bootstrap_embed.py` -- an out-of-sync
  edit fails loudly instead of drifting silently.
* To regenerate the fragments from the (possibly edited) literal, run:
  `python tools/exec_tools/_regen_sandbox_parts.py`

## Verifying the parent module still works
```
python -c "from tools.exec_tools._sandbox_bootstrap import SANDBOX_BOOTSTRAP; compile(SANDBOX_BOOTSTRAP,'x','exec'); print(len(SANDBOX_BOOTSTRAP))"
```
The literal is compiled at import time and fails closed on any syntax error.
