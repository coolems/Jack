# _part_03_audit_hook.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# STAGE 1 -- sys.addaudithook: open/enumeration/mutation/import/Popen events (primary runtime enforcement)
# Legacy source: lines 540-650 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
    # ---- 1. AUDIT HOOK: primary runtime enforcement -------------------------
    def _audit(event, args):
        try:
            if event == "open":
                target = args[0]
                if isinstance(target, int):          # fd-based open -> not a path
                    return
                s = (target.decode("utf-8", "replace")
                     if isinstance(target, bytes) else str(target))
                # Two audit shapes: builtins.open carries a mode STRING; os.open /
                # nt.open / io.FileIO carry an int FLAG word. Treating flag ints as
                # read was a verified gap -- it let nt.open(O_WRONLY|O_CREAT) write
                # anywhere READ_ROOTS reach (2026-09-17 round-B probe).
                _m = args[1] if len(args) > 1 else None
                if isinstance(_m, int):
                    _wf = 0
                    for _f in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
                        try:
                            _wf |= getattr(_jps_os, _f)
                        except Exception:
                            pass
                    is_write = bool(_m & _wf)
                else:
                    is_write = any(ch in str(_m or "r") for ch in "wax+")
                if _is_devnull(s) or _is_dev_or_pipe(s):
                    return  # device name (nul /dev/null) -- not a filesystem path
                roots = [ROOT] if is_write else READ_ROOTS
                if not _inside(s, roots):
                    _deny("open" + ("(write)" if is_write else ""), s)
            elif event in ("os.listdir", "os.scandir", "os.walk"):
                # Enumeration: read-side -> working_root + Python home.
                s = str(args[0])
                if not _inside(s, READ_ROOTS):
                    _deny(event + " (enumeration)", s)
            elif event in ("os.remove", "os.unlink", "os.rename", "os.replace",
                           "os.mkdir", "os.rmdir", "os.chmod", "os.utime",
                           "os.truncate", "os.link", "os.symlink"):
                # MUTATING ops: validate EVERY path-like positional arg (audit
                # 2026-09-16). These fire audit events in CPython core, so this
                # check holds even if user code extracts the ORIGINAL function from
                # a wrapper closure and calls it directly -- verified bypass class:
                # "builtins.open.__closure__" / "_wrap_write" cells. Shapes observed on
                # 3.12: os.rename/os.link carry (src, dst, ...), everything else has
                # the path first; ints/sentinels are skipped by the isinstance filter.
                for _a in args[:4]:
                    if isinstance(_a, (str, bytes)):
                        _s = _a.decode("utf-8", "replace") if isinstance(_a, bytes) else str(_a)
                        if not _inside(_s, [ROOT]):
                            _deny(event + " (write)", _s)
            elif event in ("shutil.copyfile", "shutil.move"):
                # HIGH-LEVEL FILE FUNCTIONS (hardening 2026-09-17): CPython audits
                # these with (src, dst). copy2's Windows fast path (_winapi.CopyFile2)
                # and move()'s cross-drive fallback fire NO "open" event -- this is
                # the net that caught the verified other-drive escape. src: read-side;
                # dst: write-side (ROOT only). Non-path args are skipped.
                _src = args[0] if len(args) > 0 else None
                _dst = args[1] if len(args) > 1 else None
                for _a, _lbl in ((_src, "source"), (_dst, "destination")):
                    if isinstance(_a, (str, bytes)):
                        _s = _a.decode("utf-8", "replace") if isinstance(_a, bytes) else str(_a)
                        _roots = [ROOT] if _lbl == "destination" else READ_ROOTS
                        if not _inside(_s, _roots):
                            _deny(event + " (" + _lbl + ")", _s)
            elif event == "import":
                name = str(args[0]).split(".")[0]
                if name in ("ctypes", "_ctypes", "cffi"):
                    _deny("import of native-code escape primitive", name)
                elif name in ("multiprocessing", "pty", "webbrowser", "venv", "ensurepip"):
                    raise PermissionError(
                        "python_exec sandbox: import of %r is blocked -- it can spawn "
                        "unsandboxed child processes or launch external programs." % name
                    )
                elif name in ("nt", "_winapi", "winreg"):
                    # Raw Win32 file/registry APIs (hardening 2026-09-17). NOTE: on
                    # CPython 3.12 Windows these modules are PRELOADED before this
                    # hook runs, so the block is a no-op there -- the raw functions
                    # themselves are patched directly in stage "RAW WIN32" below.
                    raise PermissionError(
                        "python_exec sandbox: import of %r is blocked -- raw OS-level "
                        "file/registry APIs bypass working_root containment." % name
                    )
            elif event == "subprocess.Popen":
                # Fires for EVERY spawn path (subprocess.run/Popen, asyncio
                # create_subprocess_*, popen2/os.system internals). Shape in 3.12:
                #   Windows: (None|exe_str, list2cmdline(args) or raw str, cwd, env)
                #             shell=True -> exe == ComSpec/cmd.exe (denied by basename)
                #   POSIX:   (args[0]|exe, [arglist], cwd, env); shell=True ->
                #             ['/bin/sh', '-c', <user-string>] (sh denied by basename)
                for a in args:
                    if isinstance(a, str):
                        _check_cmd(a)
                        break
                    if isinstance(a, (list, tuple)):
                        _check_cmd(list(a))
                        break
        except PermissionError:
            raise
        except Exception:
            pass  # never let a malformed event fail-open the hook

    try:
        _jps_sys.addaudithook(_audit)
    except Exception as _jps_hook_exc:
        _jps_log_exc("addaudithook: %s" % repr(_jps_hook_exc))

    try:
        _jps_log("B1: audit hook installed (open/enumeration/import/Popen checks live)")
    except Exception:
        pass


