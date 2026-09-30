# _part_04_os_patches.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# STAGE 2 -- os primitive patches + METADATA SCOPING (stat/lstat/access/readlink/os.path/pathlib) + flag-aware os.open
# Legacy source: lines 651-913 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
    # ---- 2. PATCH primitives not fully covered by an audited event ----------
    def _wrap_write(fn):
        orig = fn
        nm = getattr(orig, "__name__", "os op")
        def wrapped(path, *a, **k):
            # Validate EVERY path-like positional argument. Two-path write ops
            # (rename/replace/link/symlink) can move data OUT of the root through
            # their SECOND arg -- auditing only `path` was a verified escape
            # (audit 2026-08-31, finding F1). Non-path args (mode ints, flags,
            # booleans) are skipped by the isinstance filters.
            for _p in (path,) + tuple(a):
                if isinstance(_p, bytes):
                    s = _p.decode("utf-8", "replace")
                elif isinstance(_p, str):
                    s = _p
                else:
                    continue
                if not _inside(s, [ROOT]):
                    _deny(nm + " (write)", s)
            return orig(path, *a, **k)
        wrapped.__name__ = nm
        return wrapped

    try:
        import os as _real_os
        for _n in ("remove", "unlink", "rename", "replace", "rmdir",
                   "mkdir", "makedirs", "symlink", "link",
                   "chmod", "chown", "utime", "truncate"):
            if hasattr(_real_os, _n):
                setattr(_real_os, _n, _wrap_write(getattr(_real_os, _n)))

        try:
            _jps_log("B2a: os mutation ops wrapped")
        except Exception:
            pass

        # ---- METADATA SCOPING (hardening 2026-09-17) -------------------------
        # os.stat / lstat / access / readlink previously leaked path EXISTENCE and
        # file SIZE outside working_root (verified: exists('<OTHER_DRIVE>:/') -> True  # other-drive example,
        # stat(<system config file>).st_size -> 92). Now scoped: stat-family reads allow
        # READ_ROOTS; existence predicates answer only inside ROOT. Windows
        # subprocess PATH resolution uses CreateFile on the exe -- NOT a Python
        # stat call -- so legitimate `subprocess.run(["git", ...])` is unaffected
        # (verified 2026-09-17).
        def _wrap_meta(fn, nm, roots):
            orig = fn
            def wrapped(path, *a, **k):
                # PERF round 4: inside our own _norm() realpath walk (thread-local flag)
                # -- call the original directly; the outer check validates the result.
                if _JPS_REENTRANT is not None and getattr(_JPS_REENTRANT, "on", False):
                    return orig(path, *a, **k)
                if isinstance(path, bytes):
                    s = path.decode("utf-8", "replace")
                elif isinstance(path, str):
                    s = path
                else:
                    try:
                        s = _real_os.fspath(path)  # Path / os.PathLike -> check it too
                    except Exception:
                        return orig(path, *a, **k)  # fd or exotic arg -> original handles it
                if s is not None and (_is_devnull(s) or _is_dev_or_pipe(s)):
                    return orig(path, *a, **k)
                if not _inside(s, roots):
                    raise FileNotFoundError(
                        "[Errno 2] No such file or directory (outside working_root -- "
                        "python_exec sandbox blocks metadata access outside %s)" % root
                    )
                return orig(path, *a, **k)
            wrapped.__name__ = nm
            return wrapped

        if hasattr(_real_os, "stat"):
            _real_os.stat = _wrap_meta(getattr(_real_os, "stat"), "os.stat", READ_ROOTS)
        if hasattr(_real_os, "lstat"):
            _real_os.lstat = _wrap_meta(getattr(_real_os, "lstat"), "os.lstat", READ_ROOTS)
        # os.access: existence predicate -- ROOT only, PLUS the PATH-dir carve-out so
            # shutil.which / executable lookups keep working (they probe %PATH% dirs).
            if hasattr(_real_os, "access"):
                _orig_access = getattr(_real_os, "access")
                def _os_access(path, *a, **k):
                    # PERF round 4: inside our own _norm() realpath walk -- original directly.
                    if _JPS_REENTRANT is not None and getattr(_JPS_REENTRANT, "on", False):
                        return _orig_access(path, *a, **k)
                    s = None
                    if isinstance(path, bytes):
                        s = path.decode("utf-8", "replace")
                    elif isinstance(path, str):
                        s = path
                    else:
                        try:
                            s = _real_os.fspath(path)
                        except Exception:
                            return _orig_access(path, *a, **k)
                    if s is not None and not _is_devnull(s) and not _inside(s, [ROOT]) and not _in_path_dirs(s):
                        return False
                    return _orig_access(path, *a, **k)
                _os_access.__name__ = "os.access"
                _real_os.access = _os_access
        if hasattr(_real_os, "readlink"):
            _real_os.readlink = _wrap_meta(getattr(_real_os, "readlink"), "os.readlink", READ_ROOTS)

        try:
            _jps_log("B2b: stat/lstat/access/readlink scoped")
        except Exception:
            pass

        # os.path wrappers: exists/isfile/isdir/getsize answer only inside ROOT.
        # (genericpath calls these as module globals at call time -- patching the
        # module attributes is what takes effect; os.path.getsize references
        # os.stat dynamically, so it inherits the scoped stat automatically.)
        try:
            _op = _real_os.path
            if hasattr(_op, "exists"):
                _orig_exists = _op.exists
                def _path_exists(p):
                    if isinstance(p, (str, bytes)):
                        s = p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p)
                        if not _inside(s, [ROOT]) and not _in_path_dirs(s):
                            return False
                    return _orig_exists(p)
                _op.exists = _path_exists
            for _n in ("isfile", "isdir"):
                _orig_p = getattr(_op, _n)
                def _make_pred(orig_fn, nm):
                    def wrapped(p):
                        if isinstance(p, (str, bytes)):
                            s = p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p)
                            if not _inside(s, [ROOT]) and not _in_path_dirs(s):
                                return False
                        return orig_fn(p)
                    wrapped.__name__ = nm
                    return wrapped
                setattr(_op, _n, _make_pred(_orig_p, _n))
            if hasattr(_op, "getsize"):
                _orig_getsize = _op.getsize
                def _path_getsize(f):
                    if isinstance(f, (str, bytes)):
                        s = f.decode("utf-8", "replace") if isinstance(f, bytes) else str(f)
                        if not _inside(s, [ROOT]):
                            raise FileNotFoundError(
                                "[Errno 2] No such file or directory (outside working_root -- "
                                "python_exec sandbox blocks metadata access outside %s)" % root
                            )
                    return _orig_getsize(f)
                _op.getsize = _path_getsize
        except Exception as _jps_mod_exc:
            _jps_log_exc("os.path meta patch: %s" % repr(_jps_mod_exc))

        try:
            _jps_log("B2c: os.path wrappers patched")
        except Exception:
            pass

        # pathlib.Path metadata + mutating methods (hardening 2026-09-17).
        try:
            from pathlib import Path as _jps_Path
            def _path_str(x):
                """Best-effort path string for validation; None if not a path."""
                if isinstance(x, bytes):
                    return x.decode("utf-8", "replace")
                if isinstance(x, str):
                    return x
                try:
                    return _real_os.fspath(x)  # Path / PurePath / os.PathLike
                except Exception:
                    return None

            def _wrap_path_meta(nm, roots):
                orig = getattr(_jps_Path, nm)
                def wrapped(self, *a, **k):
                    s = _path_str(self)
                    if s is not None and not _inside(s, roots):
                        raise FileNotFoundError(
                            "[Errno 2] No such file or directory (outside working_root -- "
                            "python_exec sandbox blocks access outside %s)" % root
                        )
                    return orig(self, *a, **k)
                wrapped.__name__ = nm
                return wrapped
            for _n in ("stat", "lstat"):
                setattr(_jps_Path, _n, _wrap_path_meta(_n, READ_ROOTS))

            def _wrap_path_pred(nm):
                orig = getattr(_jps_Path, nm)
                def wrapped(self, *a, **k):
                    s = _path_str(self)
                    if s is not None and not _inside(s, [ROOT]) and not _in_path_dirs(s):
                        return False  # existence predicates: no leak outside ROOT
                    return orig(self, *a, **k)
                wrapped.__name__ = nm
                return wrapped
            for _n in ("exists", "is_file", "is_dir"):
                setattr(_jps_Path, _n, _wrap_path_pred(_n))

            def _wrap_path_write(nm):
                orig = getattr(_jps_Path, nm)
                def wrapped(self, *a, **k):
                    s = _path_str(self)
                    if s is not None and not _inside(s, [ROOT]):
                        _deny("Path." + nm + " (write)", s)
                    for _x in a:
                        xs = _path_str(_x)
                        if xs is not None and not _inside(xs, [ROOT]):
                            _deny("Path." + nm + " target (write)", xs)
                    return orig(self, *a, **k)
                wrapped.__name__ = nm
                return wrapped
            for _n in ("unlink", "rename", "replace", "mkdir"):
                setattr(_jps_Path, _n, _wrap_path_write(_n))

            # symlink_to: self must be inside ROOT; the TARGET is read-side (a link
            # may point outside -- reading through it stays scoped by the open checks).
            _orig_sym = _jps_Path.symlink_to
            def _symlink_to(self, target, *a, **k):
                s = _path_str(self)
                if s is not None and not _inside(s, [ROOT]):
                    _deny("Path.symlink_to (write)", s)
                ts = _path_str(target)
                if ts is not None and not _inside(ts, READ_ROOTS):
                    _deny("Path.symlink_to target", ts)
                return _orig_sym(self, target, *a, **k)
            _jps_Path.symlink_to = _symlink_to
        except Exception as _jps_mod_exc:
            _jps_log_exc("pathlib patch: %s" % repr(_jps_mod_exc))

        try:
            _jps_log("B2d: pathlib patched")
        except Exception:
            pass

        # low-level os.open -- flag-aware (write flags => ROOT only)
        _orig_open = _real_os.open
        def _os_open(path, flags, *a, **k):
            s = (path.decode("utf-8", "replace")
                 if isinstance(path, bytes) else path)
            wf = 0
            for _f in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
                try:
                    wf |= getattr(_real_os, _f)
                except Exception:
                    pass
            is_write = bool(flags & wf)
            if _is_devnull(s) or _is_dev_or_pipe(s):
                return _orig_open(path, flags, *a, **k)  # device name (nul) -- let the OS handle it
            if not _inside(s, [ROOT] if is_write else READ_ROOTS):
                _deny("os.open" + ("(write)" if is_write else ""), s)
            return _orig_open(path, flags, *a, **k)
        _real_os.open = _os_open

        try:
            _jps_log("B2e: os.open flag-aware patch installed")
        except Exception:
            pass

    except Exception as _jps_stage_exc:
        _jps_log_exc("stage 2 (os patches): %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B2: os/pathlib/os.open patching done")
    except Exception:
        pass


