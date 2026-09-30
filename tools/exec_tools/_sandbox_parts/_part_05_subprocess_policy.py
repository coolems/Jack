# _part_05_subprocess_policy.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# STAGE 3 -- child-process EXEC POLICY patches: Popen.__init__, os.system/popen/spawn*/exec*/fork (second net over the hook)
# Legacy source: lines 914-1053 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
    # ---- 3. CHILD-PROCESS EXEC POLICY patches (second net over the hook) --
    try:
        import subprocess as _jps_subprocess
        _orig_popen_init = _jps_subprocess.Popen.__init__

        # NOTE (audit 2026-09-16): stdin/stdout/stderr MUST be bound by name BEFORE *a
        # -- Popen.__init__ takes them as the next positional params; a caller passing
        # one positionally would otherwise collide with bufsize/executable in the
        # forward call (TypeError). Binding here is conflict-free.
        def _popen_init(self, args, bufsize=-1, executable=None,
                        stdin=None, stdout=None, stderr=None, *a, **k):
            shell_flag = bool(k.get("shell", False))
            if isinstance(args, (str, bytes)):
                _check_cmd(
                    args.decode("utf-8", "replace") if isinstance(args, bytes) else args,
                    shell_ctx=shell_flag or _IS_WIN,  # Windows: string form ALWAYS goes through cmd.exe
                )
            elif isinstance(args, (list, tuple)):
                seq = [t.decode("utf-8", "replace") if isinstance(t, (bytes, bytearray)) else str(t)
                       for t in args]
                _check_prog_base(_jps_basename(seq[0]) if seq else "", args)
            if executable is not None:
                _check_prog_base(
                    _jps_basename(executable.decode("utf-8", "replace")
                                  if isinstance(executable, bytes) else str(executable)),
                    "executable=" + repr(str(executable))[:60],
                )
            return _orig_popen_init(
                self, args, bufsize=bufsize, executable=executable,
                stdin=stdin, stdout=stdout, stderr=stderr, *a, **k
            )

        _jps_subprocess.Popen.__init__ = _popen_init
    except Exception as _jps_stage_exc:
        _jps_log_exc("stage 3 (subprocess patch): %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B3a: subprocess.Popen exec policy installed")
    except Exception:
        pass


    # os.system: audit hooks observe but cannot veto -> patch the function.
    try:
        import os as _real_os2
        if hasattr(_real_os2, "system"):
            _orig_system = _real_os2.system
            def _system(cmd):
                if isinstance(cmd, str):
                    has_op, tok = _jps_has_operator(cmd)
                    if has_op:
                        _deny_exec("shell operator in command (token %r)" % tok, cmd)
                    toks = _jps_tokens(cmd)
                    _check_prog_base(_jps_basename(toks[0]) if toks else "", cmd)
                return _orig_system(cmd)
            _real_os2.system = _system

        # os.popen: ALWAYS runs a command through a shell and returns an fd.
        # Policy-based (NOT blanket-deny): on POSIX os.system is implemented via
        # os.popen internally, so a blanket deny would break legitimate os.system()
        # calls on macOS -- the same class of failure that broke the 2026-08-31 build.
        if hasattr(_real_os2, "popen"):
            _orig_popen = _real_os2.popen
            def _popen(cmd, *a, **k):
                if isinstance(cmd, str):
                    has_op, tok = _jps_has_operator(cmd)
                    if has_op:
                        _deny_exec("shell operator in command (token %r)" % tok, cmd)
                    toks = _jps_tokens(cmd)
                    _check_prog_base(_jps_basename(toks[0]) if toks else "", cmd)
                return _orig_popen(cmd, *a, **k)
            _real_os2.popen = _popen

        # os.startfile (Windows): "open with default app" launches an EXTERNAL program
        # that then reads/writes files at OS level outside this sandbox -> deny.
        if hasattr(_real_os2, "startfile"):
            def _startfile(*a, **k):
                raise PermissionError(
                    "python_exec sandbox: os.startfile is blocked -- it launches an "
                    "external program that operates outside working_root."
                )
            _real_os2.startfile = _startfile

        # os.spawn*: allowed only for executables NOT on the denylist.
        for _n in ("spawnl", "spawnle", "spawnlp", "spawnv", "spawnve",
                   "spawnvp"):
            if hasattr(_real_os2, _n):
                _orig_spawn = getattr(_real_os2, _n)
                def _make_spawn_guard(orig_fn, nm):
                    def guarded(mode, path, *a, **k):
                        s = (path.decode("utf-8", "replace")
                             if isinstance(path, bytes) else str(path))
                        base = _jps_basename(s)
                        if _is_denied_prog(base):
                            _deny_exec("%s of %r" % (nm, s), path)
                        return orig_fn(mode, path, *a, **k)
                    guarded.__name__ = nm
                    return guarded
                setattr(_real_os2, _n, _make_spawn_guard(_orig_spawn, _n))

        # os.exec*: REPLACE this process -> kill the sandboxed child and hand control
        # to an unsandboxed one. Deny outright (POSIX-only; no-op elsewhere).
        for _n in ("execv", "execve", "execvp", "execvpe",
                   "execl", "execlp", "execle"):
            if hasattr(_real_os2, _n):
                def _make_exec_guard(nm):
                    def guarded(*a, **k):
                        raise PermissionError(
                            "python_exec sandbox: os.%s is blocked -- it would replace "
                            "the sandboxed process." % nm
                        )
                    guarded.__name__ = nm
                    return guarded
                setattr(_real_os2, _n, _make_exec_guard(_n))

        # os.fork: if we ARE the child (return 0), die immediately before any user
        # code runs there -- a forked child inherits patched builtins but could still
        # exec() in C. POSIX-only; no-op on Windows (no os.fork attribute).
        if hasattr(_real_os2, "fork"):
            _orig_fork = _real_os2.fork
            def _fork():
                pid = _orig_fork()
                if pid == 0:
                    try:
                        print("python_exec sandbox: forked child terminated (os._exit(97)).",
                              file=_jps_sys.stderr)
                    except Exception:
                        pass
                    _jps_os._exit(97)
                return pid
            _real_os2.fork = _fork
    except Exception as _jps_stage_exc:
        _jps_log_exc("stage 3b (os exec policy): %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B3: child-process exec policy done (shells/interpreters/network denied)")
    except Exception:
        pass


