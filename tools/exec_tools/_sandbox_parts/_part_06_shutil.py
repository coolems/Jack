# _part_06_shutil.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# STAGE 4 -- shutil high-level guards (copy/copy2/move/copytree/rmtree/archive writers)
# Legacy source: lines 1054-1164 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
    # ---- 4. shutil HIGH-LEVEL FILE FUNCTIONS (hardening 2026-09-17) -------
    # copy2's Windows fast path (_winapi.CopyFile2) and move()'s cross-drive
    # fallback bypass every open-level check -- verified other-drive escape. Validate
    # src/dst BEFORE delegating; the audit events above are the second net.
    try:
        import shutil as _jps_shutil

        def _sh_src_dst(fn, nm):
            orig = fn
            def wrapped(src, dst, *a, **k):
                # src: read-side (READ_ROOTS); dst: write-side (ROOT only).
                for _p, _lbl in ((src, "source"), (dst, "destination")):
                    if isinstance(_p, bytes):
                        s = _p.decode("utf-8", "replace")
                    elif isinstance(_p, str):
                        s = _p
                    else:
                        continue
                    _roots = [ROOT] if _lbl == "destination" else READ_ROOTS
                    if not _inside(s, _roots):
                        _deny(nm + " (" + _lbl + ")", s)
                return orig(src, dst, *a, **k)
            wrapped.__name__ = nm
            return wrapped

        for _n in ("copyfile", "copy", "copy2"):
            if hasattr(_jps_shutil, _n):
                setattr(_jps_shutil, _n, _sh_src_dst(getattr(_jps_shutil, _n), "shutil." + _n))

        if hasattr(_jps_shutil, "move"):
            _orig_move = _jps_shutil.move
            def _move(src, dst, *a, **k):
                for _p, _lbl in ((src, "source"), (dst, "destination")):
                    if isinstance(_p, bytes):
                        s = _p.decode("utf-8", "replace")
                    elif isinstance(_p, str):
                        s = _p
                    else:
                        continue
                    _roots = [ROOT] if _lbl == "destination" else READ_ROOTS
                    if not _inside(s, _roots):
                        _deny("shutil.move (" + _lbl + ")", s)
                return _orig_move(src, dst, *a, **k)
            _jps_shutil.move = _move

        if hasattr(_jps_shutil, "copytree"):
            _orig_ct = _jps_shutil.copytree
            def _copytree(src, dst, *a, **k):
                for _p, _lbl in ((src, "source"), (dst, "destination")):
                    if isinstance(_p, bytes):
                        s = _p.decode("utf-8", "replace")
                    elif isinstance(_p, str):
                        s = _p
                    else:
                        continue
                    _roots = [ROOT] if _lbl == "destination" else READ_ROOTS
                    if not _inside(s, _roots):
                        _deny("shutil.copytree (" + _lbl + ")", s)
                return _orig_ct(src, dst, *a, **k)
            _jps_shutil.copytree = _copytree

        if hasattr(_jps_shutil, "rmtree"):
            _orig_rmt = _jps_shutil.rmtree
            def _rmtree(path, *a, **k):
                s = (path.decode("utf-8", "replace")
                     if isinstance(path, bytes) else str(path))
                if not _inside(s, [ROOT]):
                    _deny("shutil.rmtree (write)", s)
                return _orig_rmt(path, *a, **k)
            _jps_shutil.rmtree = _rmtree

        if hasattr(_jps_shutil, "make_archive"):
            _orig_ma = _jps_shutil.make_archive
            def _make_archive(base_name, *a, **k):
                s = (base_name.decode("utf-8", "replace")
                     if isinstance(base_name, bytes) else str(base_name))
                d = _jps_os.path.dirname(s) or "."
                if not (_inside(d, [ROOT]) and _inside(s, [ROOT])):
                    _deny("shutil.make_archive (output path)", s)
                return _orig_ma(base_name, *a, **k)
            _jps_shutil.make_archive = _make_archive

        if hasattr(_jps_shutil, "unpack_archive"):
            _orig_ua = _jps_shutil.unpack_archive
            def _unpack_archive(filename, *a, **k):
                s = (filename.decode("utf-8", "replace")
                     if isinstance(filename, bytes) else str(filename))
                d = a[0] if len(a) > 0 and not isinstance(a[0], int) else k.get("extract_dir") or "."
                ds = d.decode("utf-8", "replace") if isinstance(d, bytes) else str(d)
                if not _inside(s, READ_ROOTS):
                    _deny("shutil.unpack_archive (archive)", s)
                if not _inside(ds, [ROOT]):
                    _deny("shutil.unpack_archive (extract dir)", ds)
                return _orig_ua(filename, *a, **k)
            _jps_shutil.unpack_archive = _unpack_archive

        if hasattr(_jps_shutil, "disk_usage"):
            _orig_du = _jps_shutil.disk_usage
            def _disk_usage(path):
                s = (path.decode("utf-8", "replace")
                     if isinstance(path, bytes) else str(path))
                if not _inside(s, [ROOT]):
                    raise FileNotFoundError(
                        "[Errno 2] No such file or directory (outside working_root -- "
                        "python_exec sandbox blocks metadata access outside %s)" % root
                    )
                return _orig_du(path)
            _jps_shutil.disk_usage = _disk_usage
    except Exception as _jps_stage_exc:
        _jps_log_exc("stage 4 (shutil): %s" % repr(_jps_stage_exc))

