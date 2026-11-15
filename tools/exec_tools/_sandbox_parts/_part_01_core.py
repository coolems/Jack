# _part_01_core.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# CORE -- JACK_PYEXEC_ROOT fail-closed check, READ_ROOTS (incl. every sys.path entry), _norm LRU cache, _inside prefix test, _deny, _jps_log/_jps_log_exc, devnull/PATH-dir helpers; 'def _jps_setup():' starts here
# Legacy source: lines 232-435 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
def _jps_setup():
    root = _jps_os.environ.get("JACK_PYEXEC_ROOT")
    if not isinstance(root, str) or not root.strip():
        try:
            print(
                "python_exec sandbox INIT FAILED: JACK_PYEXEC_ROOT env var missing. "
                "Refusing to run (fail-closed).",
                file=_jps_sys.stderr,
            )
        except Exception:
            pass
        _jps_os._exit(3)

    # PERF (2026-09-17 round 4 -- macOS B1->B2 hang fix): on POSIX os.path.realpath is
    # PURE PYTHON and lstats every path component; each of those lstats went through
    # our patched wrapper, which re-validated the prefix with ANOTHER full realpath ->
    # ~O(N^3) syscalls per containment check (N = path depth). On a deep Homebrew /
    # Apple-Silicon stdlib tree that made every import in stage 2 take minutes: the
    # child stalled between B1 and B2 forever. Windows never showed it because
    # ntpath.realpath is one C syscall (_getfinalpathname) -- no Python-level lstats.
    # Fix (containment semantics UNCHANGED, fail-safe):
    #   * thread-local re-entrancy flag: while OUR OWN _norm() validation is running,
    #     the patched stat/lstat/access wrappers call the ORIGINAL function directly
    #     (no nested checks -- the outer check validates the final resolved result).
    #   * LRU cache on _norm(): paths repeat constantly during stdlib imports.
    try:
        import threading as _jps_threading
        _JPS_REENTRANT = _jps_threading.local()
    except Exception:
        _JPS_REENTRANT = None  # fast path off (slower, same containment) -- never fail open

    _NORM_CACHE = {}
    _NORM_CACHE_MAX = 8192

    def _norm(p):
        key = p if isinstance(p, (str, bytes)) else None
        if key is not None:
            hit = _NORM_CACHE.get(key)
            if hit is not None:
                return hit
        try:
            if _JPS_REENTRANT is not None:
                _JPS_REENTRANT.on = True
            r = _jps_os.path.normcase(_jps_os.path.realpath(p))
        finally:
            if _JPS_REENTRANT is not None:
                _JPS_REENTRANT.on = False
        if key is not None:
            if len(_NORM_CACHE) >= _NORM_CACHE_MAX:
                try:
                    _NORM_CACHE.pop(next(iter(_NORM_CACHE)))
                except Exception:
                    pass
            _NORM_CACHE[key] = r
        return r

    ROOT = _norm(root)
    READ_ROOTS = [ROOT]
    # Python-home tree + every sys.path entry. The pyhome allowance is REQUIRED so
    # that stdlib imports keep working in the child; on macOS Homebrew/Apple-Silicon
    # installs the stdlib lives under /opt/homebrew or /usr/local which is NOT under
    # dirname(sys.executable)/sys.prefix/sys.base_prefix -- sys.path entries are the
    # portable, install-independent way to find "where this interpreter reads from".
    for _c in (
        _jps_os.path.dirname(_jps_sys.executable),
        getattr(_jps_sys, "prefix", None),
        getattr(_jps_sys, "base_prefix", None),
    ):
        if isinstance(_c, str) and _c:
            try:
                READ_ROOTS.append(_norm(_c))
            except Exception:
                pass
    for _p in list(getattr(_jps_sys, "path", [])):
        if not isinstance(_p, str):
            continue
        _abs = _p if _jps_os.path.isabs(_p) else _jps_os.path.join(root, _p)
        try:
            READ_ROOTS.append(_norm(_abs))
        except Exception:
            pass

    def _inside(p, roots):
        try:
            rp = _norm(p)
        except Exception:
            return False
        for r in roots:
            if rp == r or rp.startswith(r + _jps_os.sep):
                return True
        return False

    def _deny(what, p):
        raise PermissionError(
            "python_exec sandbox: %s of '%s' is outside working_root (%s) -- blocked."
            % (what, p, root)
        )

    # Stage markers (2026-09-17 round 3): the bootstrap is ~24 KB of patching that used
    # to be a BLACK BOX on Mac -- a child stalling inside _jps_setup() produced no C2 and
    # no other trace. Each stage now appends B* lines BY THE CHILD to .jack_pyexec_child.log
    # (the parent attaches its last lines to heartbeats/timeouts), so a hang pinpoints the
    # exact patching stage. try/except pass: logging must never break the sandbox itself.
    def _jps_log(msg):
        # ALL [PYEXEC] marker output is OPT-IN ONLY (JACK_PYEXEC_DEBUG=1 in the calling process env,
        # 2026-09-30 client-console cleanup; file part since 2026-09-18): default runs are fully
        # silent -- no stderr markers and no .jack_pyexec_child.log. python_exec forwards the flag.
        try:
            if _jps_os.environ.get("JACK_PYEXEC_DEBUG", "0") != "0":
                import sys as _jps_sys2
                print("[PYEXEC] " + msg, file=_jps_sys2.stderr, flush=True)
                _ld = _jps_os.path.join(root, ".temp")  # user policy (2026-09-18): child debug log lives in .temp/
                _jps_os.makedirs(_ld, exist_ok=True)
                with open(_jps_os.path.join(_ld, ".jack_pyexec_child.log"), "a", encoding="utf-8") as _lf2:
                    _lf2.write("[PYEXEC] " + msg + chr(10))
        except Exception:
            pass  # logging must never break the sandbox itself

    _jps_log("B0: sandbox setup started (root resolved, %d read roots)" % len(READ_ROOTS))


    # Swallowed-exception logger (2026-09-17): if any setup stage fails, the child
    # must NOT fail open silently -- record where it broke so the sandbox can be
    # repaired. Healthy runs write nothing (zero noise).
    def _jps_log_exc(where):
        try:
            import traceback as _tb
            _ld2 = _jps_os.path.join(root, ".temp")  # user policy (2026-09-18): all python_exec artifacts live in .temp/
            try:
                _jps_os.makedirs(_ld2, exist_ok=True)
            except Exception:
                pass
            with open(_jps_os.path.join(_ld2, ".jack_pyexec_sandbox_errors.txt"), "a") as _lf:
                _lf.write("\n--- %s ---\n%s" % (where, _tb.format_exc()))
        except Exception:
            pass

    # Device names are not filesystem paths (audit 2026-09-16 regression fix): on
    # Windows os.devnormalizes to \\.\NUL which is outside every root -- without this
    # exception subprocess.DEVNULL / os.open(os.devnull) broke inside the sandbox.
    def _is_devnull(p):
        s = p.decode("utf-8", "replace") if isinstance(p, (bytes, bytearray)) else str(p)
        b = s.strip().lower()
        return b in ("nul", "con", "prn", "aux", "com1", "com2", "com3",
                    "com4", "com5", "com6", "com7", "com8", "com9") or b == "/dev/null"

    # PATH-directory carve-out for EXISTENCE predicates (2026-09-17): shutil.which /
    # executable lookups probe os.path.exists + os.access on %PATH% directories that
    # live OUTSIDE working_root. The metadata scoping below would break them, so
    # exists/isfile/isdir/access are allowed to answer for paths whose DIRECTORY is a
    # PATH entry -- with TWO strict limits so this stays a non-leak:
    #   * EXACT directory match only (shutil.which probes direct children of PATH
    #     entries; it never descends into subdirectories), and
    #   * on Windows the probed name must carry an executable extension from PATHEXT
    #     (which() appends those). This matters because the OS system dir is itself a
    #     PATH entry -- without the extension rule, existence of ANY direct child of it
    #     outside working_root (e.g. a system config file) would be leakable.
    # Precomputed ONCE per run (2026-09-17 round-4 perf): the old body split + normalized
    # every PATH dir on EVERY call -- this runs from access/exists/isfile/isdir wrappers
    # for each probed path. Normalized dirs + PATHEXT extensions are built lazily on first
    # use (after _IS_WIN exists) and reused. Mid-run PATH env mutations are not re-read:
    # a stale carve-out only ever makes exists() answer False for an entry under a NEWLY
    # added dir -- the fail-closed direction (shutil.which itself reads the live env).
    _PATH_DIRS_CACHE = [None]

    def _in_path_dirs(p):
        try:
            if _PATH_DIRS_CACHE[0] is None:
                dirs, exts = set(), None
                for pd in (_jps_os.environ.get("PATH") or "").split(_jps_os.pathsep):
                    if pd:
                        try:
                            dirs.add(_norm(pd))
                        except Exception:
                            pass
                if _IS_WIN:
                    exts = [e.strip().lower() for e in (_jps_os.environ.get("PATHEXT") or ".EXE;.BAT;.CMD;.COM").split(";") if e.strip()]
                _PATH_DIRS_CACHE[0] = (dirs, exts)
            dirs, exts = _PATH_DIRS_CACHE[0]
            np = _norm(p)
            d = _jps_os.path.dirname(np)
            if d not in dirs:  # exact match only -- which() never descends subdirs
                return False
            if exts is not None:
                base = np.rsplit(_jps_os.sep, 1)[-1]
                if not any(base.endswith(e) for e in exts):
                    return False  # non-executable name under a PATH dir -> no leak
            return True
        except Exception:
            pass
        return False

    # Named-pipe / device namespace exemption (2026-09-17): asyncio subprocess
    # transports create stdio pipes via _winapi.CreateFile on '\\' .\\pipe\\...' names,
    # which are NOT filesystem paths. Exempt the whole \\.\ device/pipe namespace from
    # containment (same class as the devnull exemption). Residual: raw volume/device
    # handles (\\.\PHYSICALDRIVE0) stay reachable -- documented, not a file path.
    def _is_dev_or_pipe(p):
        s = p.decode("utf-8", "replace") if isinstance(p, (bytes, bytearray)) else str(p)
        b = s.strip().lower()
        return b.startswith('\\\\.\\') or b.startswith('//./')

