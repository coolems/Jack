# _part_03_audit_hook.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# STAGE 1 -- sys.addaudithook: open/enumeration/mutation/import/Popen events (primary runtime enforcement);
# 2026-11-14 additions: flag-aware write detection on the open event, decision-file lock, audit-level
# _winapi.CreateFile/_winapi.CreateProcess/socket.connect-connect_ex-sendto/os.system/os.spawn branches,
# _POLICED_EVENTS fail-closed on internal hook errors, addaudithook failure -> _jps_fail (exit 3),
# _check_cwd on the Popen event (args[2] on both platforms), the CreateProcess event (args[2]) and the
# os.system/os.spawn branches (those fire no Popen event, so they inherit OUR cwd)
# Legacy source: lines 540-650 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# Current literal: lines 584-821 of the embedded literal.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
    # ---- 1. AUDIT HOOK: primary runtime enforcement -------------------------
    # FAIL-CLOSED ON INTERNAL ERRORS (2026-11-14): the hook used to swallow every
    # non-PermissionError, so a broken/patched containment check silently ALLOWED the
    # operation. Now: an error while handling an event this sandbox POLICES denies that
    # operation (fail closed); an unpoliced event still passes through, because CPython
    # fires many events the sandbox does not check and denying them would break normal
    # runs. _POLICED_EVENTS lists exactly the branches below.
    _POLICED_EVENTS = frozenset((
        "open", "os.listdir", "os.scandir", "os.walk",
        "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.rmdir",
        "os.chmod", "os.utime", "os.truncate", "os.link", "os.symlink",
        "shutil.copyfile", "shutil.move", "import", "subprocess.Popen",
        "os.system", "os.spawn",
        "_winapi.CreateFile", "_winapi.CreateProcess",
    ))

    def _audit(event, args):
        _policed = event in _POLICED_EVENTS
        try:
            if event == "open":
                target = args[0]
                if isinstance(target, int):          # fd-based open -> not a path
                    return
                s = (target.decode("utf-8", "replace")
                     if isinstance(target, bytes) else str(target))
                # Two audit shapes (verified on CPython 3.12): builtins.open / io.FileIO
                # carry (path, mode STRING, mode_int); os.open / nt.open carry
                # (path, None, FLAGS_INT) -- the mode slot is None and the flag word is
                # args[2]. Reading ONLY args[1] made every os.open/nt.open WRITE look like
                # a READ at the hook level (verified: os.open(p, O_WRONLY|O_CREAT) fired
                # ('open', (p, None, 385)) and the hook computed is_write=False), so the
                # write policy and the consent-decision lock were both bypassable there.
                _m = args[1] if len(args) > 1 else None
                _fl = args[2] if len(args) > 2 else None
                _wf = 0
                for _f in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
                    try:
                        _wf |= getattr(_jps_os, _f)
                    except Exception:
                        pass
                if isinstance(_m, int):
                    is_write = bool(_m & _wf)
                elif isinstance(_fl, int):
                    is_write = bool(_fl & _wf)
                else:
                    is_write = any(ch in str(_m or "r") for ch in "wax+")
                if _is_devnull(s) or _is_dev_or_pipe(s):
                    return  # device name (nul /dev/null) -- not a filesystem path
                if is_write and _is_consent_decision(s):
                    # CONSENT SELF-APPROVAL LOCK (2026-11-14, verified escape): a thread
                    # inside the payload wrote its own <id>.decision.json and the gate read
                    # it as the user's answer. Decision files are PARENT-OWNED -> hard deny,
                    # never askable (asking about the ask would be circular).
                    _deny_decision_write("open(write)", s)
                roots = [ROOT] if is_write else READ_ROOTS
                if not _inside(s, roots):
                    _fs_deny("open" + ("(write)" if is_write else ""), s)
            elif event in ("_winapi.CreateFile",):
                # Raw Win32 handle factory, checked at the AUDIT level (2026-11-14): the
                # function wrapper below is bypassable by pulling the ORIGINAL builtin out
                # of its closure cells (verified: __closure__ hands back the pre-patch
                # CreateFile). The audit event fires inside the C function, so it holds no
                # matter how the call was reached. args = (name, access, share, creation...)
                nm = _path_str(args[0]) if args else None
                if nm is not None and _is_dev_or_pipe(nm):
                    return  # \\.\pipe\* stdio pipes -- not filesystem paths
                if nm is not None:
                    acc = args[1] if len(args) > 1 else None
                    # Verified audit shape on 3.12: (name, access, share, CREATION, flags).
                    creation = args[3] if len(args) > 3 else None
                    if _winapi_is_write(acc, creation):
                        if _is_consent_decision(nm):
                            _deny_decision_write("_winapi.CreateFile(write)", nm)
                        if not _inside(nm, [ROOT]):
                            _fs_deny("_winapi.CreateFile(write)", nm)
                    elif not _inside(nm, READ_ROOTS):
                        _fs_deny("_winapi.CreateFile(read)", nm)
            elif event == "_winapi.CreateProcess":
                # Raw spawn (2026-11-14, verified leak): a direct _winapi.CreateProcess
                # call bypasses subprocess, so the Popen event never fires for it.
                # Audit args on CPython 3.12 are (executable, <internal marker>, cwd) --
                # verified live: (None, '\x05', 'C:\\Windows'). The COMMAND LINE is NOT
                # exposed (arg1 is a 1-char marker, not the string we passed), so the audit
                # level can police the EXECUTABLE and the CWD only; command-line arguments
                # are policed by the wrapper patch in stage 6. cwd is checked FIRST here --
                # it holds for every route (subprocess, asyncio, raw calls, closure-extracted
                # originals), which is exactly what the stage-6 wrapper cannot guarantee.
                _check_cwd(args[2] if len(args) > 2 else None)
                exe = _path_str(args[0]) if args else None
                if exe is None:
                    if not _spawn_authorized():
                        raise PermissionError(
                            "python_exec sandbox: _winapi.CreateProcess with executable=None "
                            "is blocked -- the program lives in the command line, which the "
                            "audit event cannot inspect. Use subprocess.run/Popen (its exec "
                            "policy is checked)."
                        )
                    return
                if not _inside(exe, READ_ROOTS) and not _in_path_dirs(exe):
                    _fs_deny("_winapi.CreateProcess (executable)", exe)
                _check_prog_base(_jps_basename(exe), args)
            elif event in ("os.listdir", "os.scandir", "os.walk"):
                # Enumeration: read-side -> working_root + Python home.
                # _path_str() DECODES bytes (verified escape 2026-11-14): str(args[0]) on a
                # bytes path yields "b'C:\\Windows'", which _norm()/realpath() resolves as a
                # NON-EXISTENT child OF THE ROOT -> _inside() answered True -> the check
                # passed and os.listdir(b"C:\\Windows") / os.scandir(b"...") / os.walk(b"...")
                # enumerated outside working_root with no denial and no consent request.
                # Bytes is a legal path type (os.listdir accepts str/bytes/PathLike), and the
                # nt.* wrappers below already decode via _as_path_str(), so the audit-level
                # branch must decode the same way -- exactly like the "open" branch does.
                if args and isinstance(args[0], int):
                    # Enumeration BY FILE DESCRIPTOR: the fd IS the directory being
                    # enumerated, so there is no path for the hook to validate. Pre-fix the
                    # branch denied it anyway (str(fd) resolves as a non-existent child of
                    # the root); kept fail-closed, but as a HARD block with an honest message
                    # -- an fd number is not a path, so neither a consent card nor the
                    # "outside working_root" wording makes sense here.
                    raise PermissionError(
                        "python_exec sandbox: %s by file descriptor (%d) is blocked -- the "
                        "hook sees no path to validate. Enumerate by path instead."
                        % (event, args[0])
                    )
                s = _path_str(args[0])
                if s is None:
                    # NO PATH ARGUMENT: os.listdir()/os.scandir() take the path from the
                    # C level, so the audit event carries args[0] = None (verified). os.py
                    # binds os.listdir to the ORIGINAL nt.listdir object at import time
                    # (`from nt import *`), so the stage-6 nt.listdir patch cannot reach the
                    # zero-argument call -- the hook is the ONLY layer on that route. The old
                    # str(None) == "None" resolved as <root>\None, which is NOT a read root,
                    # so every default enumeration was DENIED -- including legitimate
                    # os.listdir() inside working_root (false positive, verified). It never
                    # leaked, but containment there rested on a coincidence rather than on the
                    # real directory. Resolve the inherited cwd instead (same rule as
                    # _check_cwd()): in-root default enumeration works, a cwd moved outside
                    # working_root by os.chdir() stays denied.
                    try:
                        s = _jps_os.getcwd()
                    except Exception:
                        s = ""  # undeterminable -> _inside() answers False -> fail closed
                if not _inside(s, READ_ROOTS):
                    _fs_deny(event + " (enumeration)", s)
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
                        if _is_consent_decision(_s):
                            # A mutation can REPLACE a decision file (os.rename / os.replace
                            # / shutil.move onto it) -- same self-approval escape as an
                            # open(w), so it is denied outright.
                            _deny_decision_write(event, _s)
                        if not _inside(_s, [ROOT]):
                            _fs_deny(event + " (write)", _s)
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
                            _fs_deny(event + " (" + _lbl + ")", _s)
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
            elif event in ("socket.connect", "socket.connect_ex", "socket.sendto"):
                # AUDIT-LEVEL outbound policy (2026-11-14): the socket wrappers below are
                # bypassable the same way the file wrappers are -- socket.connect.__closure__
                # hands back the pre-patch C method (verified: an extracted original
                # connected to a non-loopback target with no gate). These events fire inside
                # C, so the loopback rule and the consent gate hold however the call was
                # reached. args = (socket, address[, ...]). The admin opt-in
                # (JACK_PYEXEC_NETWORK=1 -> _NET_OPEN) installs NO restrictions, so the hook
                # honours it too -- resolved at call time like every other late name here.
                if _NET_OPEN or len(args) < 2:
                    return
                _net_gate(args[1], event.split(".", 1)[1])
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
                # cwd is args[2] on BOTH platforms (verified against Lib/subprocess.py:
                # Windows sys.audit("subprocess.Popen", executable, args, cwd, env) at
                # line 1534, POSIX the same at line 1823). Checked AFTER the command so a
                # denied program still reports the exec-policy reason first.
                _check_cwd(args[2] if len(args) > 2 else None)
                # This spawn passed the exec policy -> let the ONE following raw
                # CreateProcess(executable=None) consume the token (see _SPAWN_TOKEN).
                _grant_spawn()
            elif event == "os.system":
                # os.system is patched below, but the audit event is the net that holds
                # if the wrapper is bypassed (closure-extracted original). args=(cmd,).
                # os.system is C-level -- it fires NO Popen/CreateProcess event (verified),
                # so the cwd rule must be applied here too: the grandchild inherits OUR
                # cwd, which os.chdir() may have moved outside working_root.
                if args and isinstance(args[0], str):
                    _check_cmd(args[0], shell_ctx=True)
                    _check_cwd(None)
            elif event == "os.spawn":
                # os.spawn* (mode, path, argv...). Patched below too; this is the net.
                # Same inherited-cwd rule as os.system (no Popen event on this route).
                if len(args) >= 2:
                    _check_prog_base(_jps_basename(_path_str(args[1]) or ""), args)
                    if len(args) >= 3 and isinstance(args[2], (list, tuple)):
                        _check_cmd_paths([str(x) for x in args[2]], args)
                    _check_cwd(None)
        except PermissionError:
            raise
        except Exception as _jps_hook_err:
            # FAIL CLOSED for policed events (2026-11-14): an internal error inside a
            # containment branch must NOT let the operation through. Unpoliced events
            # still pass -- they are not filesystem/spawn/import decisions.
            if _policed:
                _jps_log_exc("audit hook (%s): %s" % (event, repr(_jps_hook_err)))
                raise PermissionError(
                    "python_exec sandbox: %s blocked -- the containment check for this "
                    "operation failed (%s), so it was denied (fail-closed)."
                    % (event, repr(_jps_hook_err)[:120])
                )
            pass  # malformed/unpoliced event -- never fail-open unrelated CPython events

    try:
        _jps_sys.addaudithook(_audit)
    except Exception as _jps_hook_exc:
        # FAIL CLOSED (2026-11-14, verified): the hook is the PRIMARY enforcement layer --
        # enumeration, raw Win32 and spawn checks exist ONLY here. Swallowing this left the
        # child fully unsandboxed with rc=0. A hook that cannot install means no sandbox.
        _jps_fail("audit hook could not be installed")

    try:
        _jps_log("B1: audit hook installed (open/enumeration/import/Popen checks live)")
    except Exception:
        pass


