# _part_07_archives.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# STAGE 5 -- archive + compressed-file writers (zipfile/tarfile member traversal, gzip/bz2/lzma mode-aware open)
# Legacy source: lines 1165-1301 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
    # ---- 5. ARCHIVE + COMPRESSED-FILE WRITERS (hardening 2026-09-17) ------
    try:
        import zipfile as _jps_zipfile

        def _zip_member_ok(member):
            """Member path containment: absolute or '..' traversal -> deny."""
            name = member if isinstance(member, str) else getattr(member, "filename", None)
            if not isinstance(name, str) or not name:
                return True  # let the original raise its own error
            norm = name.replace("\\", "/")
            parts = [p for p in norm.split("/") if p and p != "."]
            if any(p == ".." for p in parts):
                _deny("zip member path traversal", name)
            if name.startswith(("/", "\\")) or (len(name) >= 2 and name[1] == ":"):
                _deny("absolute zip member path", name)
            return True

        _orig_zf_init = _jps_zipfile.ZipFile.__init__
        def _zf_init(self, file=None, mode="r", *a, **k):
            if isinstance(file, (str, bytes)):
                s = file.decode("utf-8", "replace") if isinstance(file, bytes) else str(file)
                # URL forms ('https://...', 'ftp://...') are a remote-read channel.
                _sl = s.find("/")
                if ":" in s and (_sl == -1 or s.index(":") < _sl):
                    _deny("zipfile.ZipFile (URL scheme)", s)
                is_write = any(ch in str(mode) for ch in "wax+")
                roots = [ROOT] if is_write else READ_ROOTS
                if not _inside(s, roots):
                    _deny("zipfile.ZipFile" + ("(write)" if is_write else ""), s)
            return _orig_zf_init(self, file, mode, *a, **k)
        _jps_zipfile.ZipFile.__init__ = _zf_init

        _orig_extractall = _jps_zipfile.ZipFile.extractall
        def _extractall(self, path=None, members=None, pwd=None, *a, **k):
            p = path if path is not None else "."
            ps = (p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p))
            if not _inside(ps, [ROOT]):
                _deny("zipfile extractall destination", ps)
            for m in (members or self.namelist()):
                _zip_member_ok(m)
            return _orig_extractall(self, path, members, pwd, *a, **k)
        _jps_zipfile.ZipFile.extractall = _extractall

        _orig_extract = _jps_zipfile.ZipFile.extract
        def _extract(self, member, path=None, pwd=None):
            p = path if path is not None else "."
            ps = (p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p))
            if not _inside(ps, [ROOT]):
                _deny("zipfile extract destination", ps)
            _zip_member_ok(member)
            return _orig_extract(self, member, path, pwd)
        _jps_zipfile.ZipFile.extract = _extract

    except Exception as _jps_stage_exc:
        _jps_log_exc("stage 5 (zipfile): %s" % repr(_jps_stage_exc))

    try:
        import tarfile as _jps_tarfile

        def _tar_member_ok(name):
            if not isinstance(name, str) or not name:
                return True
            norm = name.replace("\\", "/")
            parts = [p for p in norm.split("/") if p and p != "."]
            if any(p == ".." for p in parts):
                _deny("tar member path traversal", name)
            if name.startswith(("/", "\\")) or (len(name) >= 2 and name[1] == ":"):
                _deny("absolute tar member path", name)
            return True

        _orig_tf_open = _jps_tarfile.TarFile.open
        def _tf_open(name=None, mode="r", *a, **k):
            if isinstance(name, (str, bytes)):
                s = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
                is_write = any(ch in str(mode) for ch in "wax+")
                roots = [ROOT] if is_write else READ_ROOTS
                if not _inside(s, roots):
                    _deny("tarfile.TarFile" + ("(write)" if is_write else ""), s)
            return _orig_tf_open(name, mode, *a, **k)
        _jps_tarfile.TarFile.open = _tf_open

        _orig_textractall = _jps_tarfile.TarFile.extractall
        def _textractall(self, path=None, members=None, *a, **k):
            p = path if path is not None else "."
            ps = (p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p))
            if not _inside(ps, [ROOT]):
                _deny("tarfile extractall destination", ps)
            for m in members or []:
                nm = m.name if hasattr(m, "name") else m
                _tar_member_ok(nm)
            return _orig_textractall(self, path, members, *a, **k)
        _jps_tarfile.TarFile.extractall = _textractall

        _orig_textract = _jps_tarfile.TarFile.extract
        def _textract(self, member, path=None, *a, **k):
            p = path if path is not None else "."
            ps = (p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p))
            if not _inside(ps, [ROOT]):
                _deny("tarfile extract destination", ps)
            nm = member.name if hasattr(member, "name") else member
            _tar_member_ok(nm)
            return _orig_textract(self, member, path, *a, **k)
        _jps_tarfile.TarFile.extract = _textract

    except Exception as _jps_stage_exc:
        _jps_log_exc("stage 5b (tarfile): %s" % repr(_jps_stage_exc))

    # gzip / bz2 / lzma .open -- mode-aware (write => ROOT only).
    for _modname in ("gzip", "bz2", "lzma"):
        try:
            _m = __import__(_modname)
            if hasattr(_m, "open"):
                _orig_co = getattr(_m, "open")
                def _make_comp_guard(orig_fn, nm):
                    def wrapped(filename=None, mode="rb", *a, **k):
                        f = filename if filename is not None else (a[0] if a else k.get("filename"))
                        md = mode if isinstance(mode, str) and mode else "rb"
                        if isinstance(f, (str, bytes)):
                            s = f.decode("utf-8", "replace") if isinstance(f, bytes) else str(f)
                            is_write = any(ch in md for ch in "wax+")
                            roots = [ROOT] if is_write else READ_ROOTS
                            if not _inside(s, roots):
                                _deny(nm + ".open" + ("(write)" if is_write else ""), s)
                        return orig_fn(filename, mode, *a, **k) if filename is not None \
                            else orig_fn(*a, **k)
                    wrapped.__name__ = nm
                    return wrapped
                setattr(_m, "open", _make_comp_guard(_orig_co, _modname))
        except Exception as _jps_mod_exc:
            _jps_log_exc("compressed writer (%s): %s" % (_modname, repr(_jps_mod_exc)))

    try:
        _jps_log("B4: shutil/archive/compressed-file guards done")
    except Exception:
        pass


