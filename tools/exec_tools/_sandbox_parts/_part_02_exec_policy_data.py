# _part_02_exec_policy_data.py -- PROVENANCE FRAGMENT of _sandbox_bootstrap.SANDBOX_BOOTSTRAP
#
# STAGE 0 -- child-process EXEC POLICY data + helpers (denylists, tokenizers, _check_cmd, interpreter detection)
# Legacy source: lines 436-539 of the pre-split single-file SANDBOX_BOOTSTRAP string.
# TEXT FRAGMENT, not an importable module. PROVENANCE ONLY (2026-09-23):
# _sandbox_bootstrap.py keeps SANDBOX_BOOTSTRAP as an IN-FILE literal and performs NO
# disk reads at import time -- the CLIENT execs that source in-memory where no repo
# folder exists, so a runtime dependency on this folder would kill python_exec on every
# client. This file documents ONE stage of the literal for auditability; regenerate it
# with:  python tools/exec_tools/_regen_sandbox_parts.py
# Keep everything below the marker VERBATIM (no new imports, no indentation changes).
# === FRAGMENT START (verbatim) ===
    # ---- 0. CHILD-PROCESS EXEC POLICY data + helpers (2026-09-15 Mac fix) -----
    # NOTE (2026-09-17): the former `hasattr(os, "nt")` check was FALSE on CPython 3.12
    # Windows -- os.py does `from nt import *` without binding an `os.nt` attribute.
    # That silently disabled string-command operator checks AND skipped the whole
    # raw-Win32 patch stage (verified: _winapi.CopyFile2 + nt.stat stayed unpatched).
    # os.name == "nt" is the canonical, version-stable Windows check.
    _IS_WIN = (_jps_os.name == "nt")
    # Basenames that must never be spawned: shells, interpreters (a second
    # unsandboxed Python is the same escape with extra steps), and network
    # transfer tools (exfiltration channels; use the download_file tool).
    _DENY_PROGS = frozenset((
        "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
        "wscript", "wscript.exe", "cscript", "cscript.exe", "conhost", "conhost.exe",
        "sh", "bash", "zsh", "dash", "ksh", "ash", "tcsh", "csh",
        "python", "python2", "python3", "pythonw", "py", "node", "nodejs",
        "perl", "ruby", "php", "java",
        "curl", "wget", "ftp", "lftp", "sftp", "scp", "rsync", "tftp", "telnet",
    ))
    # Shell operators that let one string chain/redirect into arbitrary commands.
    _SHELL_OPS = frozenset("|&;<>(){}$`\\\n")

    def _deny_exec(what, detail):
        raise PermissionError(
            "python_exec sandbox: child-process spawn blocked -- %s (%s). "
            "Shells, interpreters and network tools cannot be spawned from "
            "python_exec; filesystem access stays inside working_root." % (what, str(detail)[:120])
        )

    def _jps_basename(s):
        s = str(s)
        if '"' in s:
            s = s.strip('"')
        return s.replace("/", "\\").rsplit("\\", 1)[-1].lower() if "\\" in s \
            else s.rsplit("/", 1)[-1].lower()

    def _jps_tokens(cmdline):
        """Quote-aware whitespace tokenization (cmd/sh quoting, simplified)."""
        toks, cur, in_q = [], "", False
        for ch in cmdline:
            if ch == '"':
                in_q = not in_q
            elif ch.isspace() and not in_q:
                if cur:
                    toks.append(cur)
                    cur = ""
            else:
                cur += ch
        if cur:
            toks.append(cur)
        return toks

    def _jps_has_operator(cmdline):
        for tok in _jps_tokens(cmdline):
            if any(ch in _SHELL_OPS for ch in tok):
                return True, tok[:40]
        return False, None

    # Interpreter basenames with version suffixes / .exe: python.exe, python3.12,
    # nodejs, perl5 ... -- a spawned interpreter is an unsandboxed second runtime
    # regardless of its exact filename (verified gap 2026-09-15: sys.executable on
    # Windows is 'python.exe', which the exact-match list missed).
    _INTERP_PREFIXES = ("python", "nodejs", "node", "perl", "ruby", "php")

    def _is_interpreter(b):
        for pre in _INTERP_PREFIXES:
            if b == pre or b.startswith(pre):
                rest = b[len(pre):]
                # allow version suffixes (3, 3.12) and pythonw; reject other letters
                ok_rest = rest in ("", "w") or all(ch.isdigit() or ch in "._" for ch in rest)
                if ok_rest:
                    return True
        return b == "java"

    def _is_denied_prog(base):
        b = base[:-4] if base.endswith(".exe") else base
        return (base in _DENY_PROGS or b in _DENY_PROGS) or _is_interpreter(b)

    def _check_prog_base(base, ctx):
        if base and _is_denied_prog(base):
            _deny_exec("program %r is a shell/interpreter/network tool" % base, ctx)

    def _check_cmd(cmdline, shell_ctx=False):
        """Validate one command for spawning. `cmdline` is a list or a string."""
        if isinstance(cmdline, (bytes, bytearray)):
            cmdline = bytes(cmdline).decode("utf-8", "replace")
        if not isinstance(cmdline, str) and not isinstance(cmdline, (list, tuple)):
            return  # non-command arg shape -> let the OS fail it naturally
        if isinstance(cmdline, str):
            # A string command is shell-interpreted on Windows ALWAYS (CPython
            # wraps it in `comspec /c "..."` before CreateProcess) and on POSIX
            # only when the caller passed shell=True.
            sc = True if _IS_WIN else bool(shell_ctx)
            if sc:
                has_op, tok = _jps_has_operator(cmdline)
                if has_op:
                    _deny_exec("shell operator in command (token %r)" % tok, cmdline)
            toks = _jps_tokens(cmdline)
            _check_prog_base(_jps_basename(toks[0]) if toks else "", cmdline)
        else:
            seq = [t.decode("utf-8", "replace") if isinstance(t, (bytes, bytearray)) else str(t)
                   for t in cmdline]
            prog = seq[0] if seq else ""
            _check_prog_base(_jps_basename(prog), cmdline)

