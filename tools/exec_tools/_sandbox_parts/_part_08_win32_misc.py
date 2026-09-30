# _part_08_win32_misc.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# STAGE 6 -- raw Win32 (_winapi/nt) file primitives + builtins.open containment + loopback-only sockets + importlib/sqlite3 guards; last line ends _jps_setup()
# Legacy source: lines 1302-1663 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
    # ---- 6. RAW WIN32 FILE PRIMITIVES (Windows; hardening 2026-09-17) -----
    # nt / _winapi are PRELOADED into sys.modules before this setup runs (verified),
    # so the "import" audit block is a no-op for them -- user code can `import nt`
    # and call raw Win32 functions directly. Verified residual holes closed here:
    #   * _winapi.CopyFile2(src, '<OTHER_DRIVE>:/x') succeeded (the shutil.copy2 fast path)
    #   * nt.stat(<SYSTEM_FILE>).st_size leaked file metadata (system file outside root)
    #   * nt.open(path, O_WRONLY|O_CREAT) write-flags were misread as read mode
    # Fix: patch the raw functions themselves. Guarded by hasattr so this whole
    # block is a no-op on macOS/Linux.
    try:
        if _IS_WIN:
            import nt as _nt

            def _as_path_str(x):
                """Best-effort path string (str/bytes/os.PathLike); None otherwise."""
                if isinstance(x, bytes):
                    return x.decode("utf-8", "replace")
                if isinstance(x, str):
                    return x
                try:
                    return _jps_os.fspath(x)
                except Exception:
                    return None

            # nt.open -- FLAG-AWARE (write flags => ROOT only; read => READ_ROOTS).
            _orig_nt_open = _nt.open
            def _nt_open(path, flags=0, *a, **k):
                s = _as_path_str(path)
                if s is not None:
                    wf = 0
                    for _f in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
                        try:
                            wf |= getattr(_nt, _f)
                        except Exception:
                            pass
                    is_write = bool(flags & wf) if isinstance(flags, int) else True
                    if not _inside(s, [ROOT] if is_write else READ_ROOTS):
                        _deny("nt.open" + ("(write)" if is_write else ""), s)
                return _orig_nt_open(path, flags, *a, **k)
            _nt.open = _nt_open

            # nt.stat / nt.lstat -- raw metadata; scope like os.stat (READ_ROOTS).
            for _n in ("stat", "lstat"):
                if hasattr(_nt, _n):
                    def _make_ntmeta(nm, orig_fn):
                        def wrapped(path, *a, **k):
                            s = _as_path_str(path)
                            if s is not None and not _inside(s, READ_ROOTS):
                                raise FileNotFoundError(
                                    "[Errno 2] No such file or directory (outside working_root -- "
                                    "python_exec sandbox blocks metadata access outside %s)" % root
                                )
                            return orig_fn(path, *a, **k)
                        wrapped.__name__ = nm
                        return wrapped
                    setattr(_nt, _n, _make_ntmeta("nt." + _n, getattr(_nt, _n)))

            # nt.access -- existence predicate; ROOT only (no leak outside root).
            if hasattr(_nt, "access"):
                _orig_ntacc = _nt.access
                def _nt_access(path, *a, **k):
                    s = _as_path_str(path)
                    if s is not None and not _inside(s, [ROOT]) and not _in_path_dirs(s):
                        return False
                    return _orig_ntacc(path, *a, **k)
                _nt.access = _nt_access

            # _winapi -- the raw Win32 handle factory + copy fast path.
            try:
                import _winapi as _wapi

                # CopyFile2 -- the shutil.copy2 Windows fast path (verified other-drive escape).
                if hasattr(_wapi, "CopyFile2"):
                    _orig_cf2 = _wapi.CopyFile2
                    def _cf2(src, dst, *a, **k):
                        ss = _as_path_str(src)
                        ds = _as_path_str(dst)
                        if ss is not None and not _inside(ss, READ_ROOTS):
                            _deny("_winapi.CopyFile2 (source)", ss)
                        if ds is not None and not _inside(ds, [ROOT]):
                            _deny("_winapi.CopyFile2 (destination)", ds)
                        return _orig_cf2(src, dst, *a, **k)
                    _wapi.CopyFile2 = _cf2

                # CreateFile -- raw handle factory. Write/modify access => ROOT only;
                # read-only handles => READ_ROOTS. Fail-closed: if the access value
                # cannot be determined (unusual arg shape), treat it as a write.
                if hasattr(_wapi, "CreateFile"):
                    _WACC = 0
                    for _c in ("GENERIC_WRITE", "FILE_GENERIC_WRITE", "FILE_ALL_ACCESS", "DELETE"):
                        try:
                            _WACC |= getattr(_wapi, _c)
                        except Exception:
                            pass
                    _orig_cfile = _wapi.CreateFile
                    def _cfile(name, *a, **k):
                        s = _as_path_str(name)
                        # Named pipes / device namespaces are not filesystem paths (asyncio stdio
                        # transports create them via CreateFile) -- exempt from containment.
                        if s is not None and _is_dev_or_pipe(s):
                            return _orig_cfile(name, *a, **k)
                        if s is not None:
                            acc = k.get("dwDesiredAccess")
                            if acc is None and a and isinstance(a[0], int):
                                acc = a[0]
                            if not isinstance(acc, int):
                                acc = -1  # undetermined -> fail closed (write policy)
                            is_write = (acc & _WACC) != 0 if (_WACC and acc >= 0) else True
                            if not _inside(s, [ROOT] if is_write else READ_ROOTS):
                                _deny("_winapi.CreateFile" + ("(write)" if is_write else ""), s)
                        return _orig_cfile(name, *a, **k)
                    _wapi.CreateFile = _cfile
            except Exception as _jps_mod_exc:
                _jps_log_exc("raw win32 (_winapi): %s" % repr(_jps_mod_exc))
    except Exception as _jps_stage_exc:
        _jps_log_exc("stage 6 (raw win32): %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B5: raw Win32 stage done (%s)" % ("patched" if _IS_WIN else "skipped, not Windows"))
    except Exception:
        pass


    # builtins.open -- defense-in-depth on top of the audit hook (catches builds or
    # contexts where addaudithook is unavailable/removed by user code before an open).
    try:
        import builtins as _jps_builtins
        _orig_builtin_open = _jps_builtins.open
        def _builtin_open(file, mode="r", *a, **k):
            if not isinstance(file, (str, bytes)):
                return _orig_builtin_open(file, mode, *a, **k)  # fd or path-like -> audit hook covers it
            s = file.decode("utf-8", "replace") if isinstance(file, bytes) else file
            is_write = any(ch in str(mode) for ch in "wax+")
            if _is_devnull(s) or _is_dev_or_pipe(s):
                return _orig_builtin_open(file, mode, *a, **k)  # device name (nul /dev/null)
            if not _inside(s, [ROOT] if is_write else READ_ROOTS):
                _deny("open" + ("(write)" if is_write else ""), s)
            return _orig_builtin_open(file, mode, *a, **k)
        _jps_builtins.open = _builtin_open
    except Exception as _jps_stage_exc:
        _jps_log_exc("builtins.open patch: %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B6: builtins.open containment installed")
    except Exception:
        pass


    # socket.connect / create_connection -- LOOPBACK ONLY (2026-09-15): any outbound
    # connection from inside python_exec is an exfiltration channel. Localhost probes
    # keep working; everything else is denied at the moment of connect, before a byte
    # leaves the machine.
    try:
        import socket as _jps_sock

        def _is_loopback(addr):
            # Defensive: non-tuple "addresses" (caller errors) pass through to the
            # original method so CPython raises its own TypeError, not ours.
            if not isinstance(addr, (tuple, list)):
                return True
            host = str(addr[0]) if addr else ""
            h = host.strip("[]").lower()
            return h == "localhost" or h.startswith("127.") or h == "::1"

        # NOTE: unique names (_sock_*) on purpose -- the sqlite3 block below also
        # defines _connect/_orig_connect in this SAME scope; Python closures resolve
        # free variables at call time, so a shared name would make the socket wrapper
        # call sqlite3.connect (verified bug 2026-09-15: broke asyncio/socketpair).
        _sock_orig_connect = _jps_sock.socket.connect
        def _sock_connect(self, address):
            if not _is_loopback(address):
                raise PermissionError(
                    "python_exec sandbox: socket connect to %r is blocked -- only "
                    "loopback (localhost) connections are allowed inside python_exec."
                    % (address,)
                )
            return _sock_orig_connect(self, address)
        _jps_sock.socket.connect = _sock_connect

        # NOTE (audit 2026-09-16): socket.connect is a C method and connect_ex /
        # sendto / sendall are SEPARATE methods -- the .connect-only patch left an
        # unconnected-UDP sendto and a connect_ex fully open (verified probes V1/V2).
        if hasattr(_jps_sock.socket, "connect_ex"):
            _sock_orig_cex = _jps_sock.socket.connect_ex
            def _sock_connect_ex(self, address):
                if not _is_loopback(address):
                    raise PermissionError(
                        "python_exec sandbox: socket connect_ex to %r is blocked -- only "
                        "loopback (localhost) connections are allowed inside python_exec."
                        % (address,)
                    )
                return _sock_orig_cex(self, address)
            _jps_sock.socket.connect_ex = _sock_connect_ex

        def _sock_find_addr(a):
            # sendto data[, flags][, address] -- the first non-int positional arg
            # after data is the address (flags are ints; addresses are tuples).
            for x in a[1:]:
                if not isinstance(x, int):
                    return x
            return None

        for _m in ("sendto", "sendall"):
            if hasattr(_jps_sock.socket, _m):
                _orig_m = getattr(_jps_sock.socket, _m)
                def _make_send_guard(orig_fn, nm):
                    def guarded(self, *a, **k):
                        addr = k.get("address") if "address" in k else (k.get("addr") if "addr" in k else None)
                        if addr is None:
                            addr = _sock_find_addr(a) if nm == "sendto" else None
                        if addr is not None and not _is_loopback(addr):
                            raise PermissionError(
                                "python_exec sandbox: socket %s to %r is blocked -- only "
                                "loopback (localhost) addresses are allowed inside python_exec."
                                % (nm, addr)
                            )
                        return orig_fn(self, *a, **k)
                    guarded.__name__ = nm
                    return guarded
                setattr(_jps_sock.socket, _m, _make_send_guard(_orig_m, _m))

        if hasattr(_jps_sock.socket, "sendmsg"):
            _sock_orig_sm = _jps_sock.socket.sendmsg
            def _sock_sendmsg(self, *a, **k):
                addr = k.get("addr", a[1] if len(a) > 1 else None)
                if isinstance(addr, tuple) and not _is_loopback(addr):
                    raise PermissionError(
                        "python_exec sandbox: socket sendmsg to %r is blocked -- only "
                        "loopback (localhost) addresses are allowed inside python_exec."
                        % (addr,)
                    )
                return _sock_orig_sm(self, *a, **k)
            _jps_sock.socket.sendmsg = _sock_sendmsg

        if hasattr(_jps_sock.socket, "sendfile"):
            # sendfile streams a LOCAL FILE to the peer -- content exfiltration.
            _sock_orig_sf = _jps_sock.socket.sendfile
            def _sock_sendfile(self, *a, **k):
                raise PermissionError(
                    "python_exec sandbox: socket sendfile is blocked -- it would stream "
                    "a local file to a remote peer outside working_root."
                )
                return _sock_orig_sf(self, *a, **k)
            _jps_sock.socket.sendfile = _sock_sendfile

        if hasattr(_jps_sock, "create_connection"):
            _sock_orig_cc = _jps_sock.create_connection
            def _sock_cc(address, *a, **k):
                if not _is_loopback(address):
                    raise PermissionError(
                        "python_exec sandbox: socket create_connection to %r is blocked "
                        "-- only loopback (localhost) connections are allowed inside python_exec."
                        % (address,)
                    )
                return _sock_orig_cc(address, *a, **k)
            _jps_sock.create_connection = _sock_cc
    except Exception as _jps_stage_exc:
        _jps_log_exc("socket patch: %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B7: socket loopback-only policy installed")
    except Exception:
        pass


    # importlib loaders used DIRECTLY (no "import" statement): loading a native
    # extension fires no "import" audit event (verified 2026-09-16, F8: ExtensionFileLoader
    # on _ctypes.pyd loaded with zero events -- full Win32 API escape). The file lives
    # under the Python home tree, which IS a legitimate READ_ROOT, so path checks alone
    # cannot catch it. Policy here mirrors the "import" event: blocked native-code
    # primitives (ctypes/_ctypes/cffi) are denied by module name, and any load whose file
    # stem does not match the module name (aliased direct loads) is denied. Ordinary
    # stdlib imports construct loaders with correlated name/path -- unaffected.
    try:
        import importlib._bootstrap_external as _jps_ibext
        if hasattr(_jps_ibext, "ExtensionFileLoader"):
            _orig_ext_exec = _jps_ibext.ExtensionFileLoader.exec_module
            def _ext_exec(self, module):
                try:
                    name = str(getattr(self, "name", "") or "")
                    top = name.split(".")[0]
                    # Same native-code blocklist as the "import" audit event: a direct
                    # loader load of _ctypes.pyd fires no import event (verified F8), so
                    # apply the identical policy here.
                    if top in ("ctypes", "_ctypes", "cffi"):
                        _deny("native extension load of blocked native-code primitive", name)
                    p = getattr(self, "path", None)
                    if isinstance(p, (str, bytes)):
                        s = p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p)
                        # Consistency check: a REAL import always constructs the loader
                        # with name == file stem (FileFinder correlation). An aliased
                        # direct load (ExtensionFileLoader("harmless", ".../_ctypes.pyd"))
                        # fails it. Only genuine extension modules can exec anyway
                        # (they need a PyInit_<name> export), so the surviving allowed set
                        # equals what plain `import <name>` could already do.
                        try:
                            import importlib.machinery as _jps_mach
                            base = s.replace("\\", "/").rsplit("/", 1)[-1]
                            stem = None
                            for _suf in _jps_mach.EXTENSION_SUFFIXES:
                                if base.endswith(_suf):
                                    stem = base[: len(base) - len(_suf)]
                                    break
                            last = name.rsplit(".", 1)[-1]
                            if stem is not None and stem != last:
                                _deny("native extension load: file/module name mismatch (aliased direct load)", s)
                        except PermissionError:
                            raise
                        except Exception:
                            pass  # never fail-open the import machinery
                except PermissionError:
                    raise
                except Exception as _jpe2:
                    _jps_log_exc("ExtensionFileLoader guard: %s" % repr(_jpe2))
                return _orig_ext_exec(self, module)
            _jps_ibext.ExtensionFileLoader.exec_module = _ext_exec
    except Exception as _jps_stage_exc:
        _jps_log_exc("importlib loader patch: %s" % repr(_jps_stage_exc))

    # sqlite3.connect -- its C VFS bypasses the "open" audit event; validate path.
    try:
        import sqlite3 as _sqlite3
        _orig_connect = _sqlite3.connect
        def _connect(database, *a, **k):
            if isinstance(database, (str, bytes)):
                s = database.decode("utf-8", "replace") \
                    if isinstance(database, bytes) else database
                # ':memory:' & friends live in-process -- no path check needed.
                # URI forms ("file:...?mode=rw") must have their PATH component
                # validated: a bare startswith(":") pass-through let
                # 'file:/outside.db' escape the sandbox (audit 2026-08-23, F4).
                if s.startswith("file:"):
                    _body = s[5:].split("#", 1)[0]
                    p = _body.split("?", 1)[0]
                    try:
                        from urllib.parse import unquote as _jps_unq
                        p = _jps_unq(p)
                    except Exception:
                        pass
                    ro = False
                    if "?" in _body:
                        for _kv in _body.split("?", 1)[1].split("&"):
                            if "=" in _kv and _kv.split("=", 1)[0].lower() == "mode":
                                ro = _kv.split("=", 1)[1] in ("ro", "immutable")
                elif s.startswith(":"):
                    return _orig_connect(database, *a, **k)
                else:
                    p, ro = s, False
                if not _inside(p, READ_ROOTS if ro else [ROOT]):
                    _deny("sqlite3.connect (database path)", s)
            return _orig_connect(database, *a, **k)
        _sqlite3.connect = _connect
    except Exception as _jps_stage_exc:
        _jps_log_exc("sqlite3 patch: %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B8: importlib/sqlite3 guards done")
    except Exception:
        pass



