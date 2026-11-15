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
            # (2026-10-24) consentable: asks the local user when a UI relay is live.
            _exec_gate("program %r is a shell/interpreter/network tool" % base, ctx)

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
                    # (2026-10-24) consentable: asks the local user when a UI relay is live.
                    _exec_gate("shell operator in command (token %r)" % tok, cmdline)
            toks = _jps_tokens(cmdline)
            _check_prog_base(_jps_basename(toks[0]) if toks else "", cmdline)
        else:
            seq = [t.decode("utf-8", "replace") if isinstance(t, (bytes, bytearray)) else str(t)
                   for t in cmdline]
            prog = seq[0] if seq else ""
            _check_prog_base(_jps_basename(prog), cmdline)

    # ---- OUT-OF-SANDBOX SUBPROCESS CONSENT GATE (2026-10-24) ------------------
    # A denied child-process spawn (shell / interpreter / network tool, or a shell
    # operator in a string command) no longer dies instantly when a UI relay is live:
    # the child writes a request file to <working_root>/.temp/.jack_netreq/<id>.json
    # with kind="subprocess" and waits (same budget as the network/file gates) for the
    # parent's decision file. The parent relays it to the chat UI as an approval card
    # ("Allow this spawn" / "Deny"). Without a relay (JACK_PYEXEC_NET_CONSENT=0)
    # behavior is byte-for-byte identical to the original instant hard block -- same
    # message, no waiting, no files. Decisions are cached per command signature for
    # THIS run only; each DISTINCT spawn gets its own ask (a user who allowed one curl
    # URL must still decide about a different target). Fail-closed everywhere: timeout
    # or any internal error denies. NOTE: the relay infrastructure (_NET_CONSENT_ON /
    # _NET_REQ_DIR / _NET_LOCK / _jps_time) is DEFINED LATER in this same function and
    # resolved at CALL time -- exactly like _fs_deny() does, so no forward-reference bug.
    _EXEC_DECIDED = {}   # command signature -> "allow" | "deny" (this run only)
    _EXEC_INFLIGHT = {}  # command signature -> request_id       (dedupe concurrent asks)
    def _exec_gate(what, detail):
        """Spawn denial with the consent option: instant hard block unless a UI relay is
        live, in which case ASK THE LOCAL USER (fail-closed on timeout/error)."""
        if not _NET_CONSENT_ON or _NET_REQ_DIR is None:
            _deny_exec(what, detail)  # original message -- no waiting
        # (2026-10-24 fix: duplicate consent cards) EVERY spawn passes TWO gate
        # checkpoints in sequence -- the audit hook (which sees CPython's
        # list2cmdline STRING form, e.g. cmd /c "echo X") and the patched
        # Popen.__init__ (which sees the caller's ORIGINAL LIST, ['cmd', '/c',
        # 'echo X']). With sig = str(detail) those produced two DIFFERENT
        # signatures -> two request files -> TWO cards for ONE spawn. Normalize
        # to a canonical token form so both checkpoints share one signature and
        # one decision: shlex.split of the actual command (string or list),
        # lowercased program basename, quoted args -- e.g. 'cmd /c "echo x"'.
        # NOTE: shlex is deliberately NOT used here -- its POSIX backslash
        # unescaping mangles Windows paths (C:\Windows -> c:windows), which
        # would re-split list-form vs string-form signatures. The existing
        # quote-aware _jps_tokens() splitter treats backslashes as literals.
        try:
            if isinstance(detail, (list, tuple)):
                _sig_toks = [str(t) for t in detail]
            else:
                _sig_toks = _jps_tokens(str(detail))
            _prog = _jps_basename(_sig_toks[0]) if _sig_toks else ""
            sig = (_prog + " " + " ".join('"%s"' % t for t in _sig_toks[1:])).strip()[:200]
        except Exception:
            sig = str(detail)[:120]
        with _NET_LOCK:
            d = _EXEC_DECIDED.get(sig)
            rid = _EXEC_INFLIGHT.get(sig)
            if d is None and rid is None:
                import uuid as _jps_uuid
                rid = _jps_uuid.uuid4().hex[:12]
                req_path = _jps_os.path.join(_NET_REQ_DIR, rid + ".json")
                try:
                    with open(req_path, "w", encoding="utf-8") as _rf:
                        _jps_json.dump({"request_id": rid, "kind": "subprocess",
                                        "what": what, "command": sig}, _rf)
                except Exception:
                    _deny_exec(what, detail)  # cannot even write the request -> fail closed
                _EXEC_INFLIGHT[sig] = rid
        if d is None:
            deadline = _jps_time.monotonic() + _NET_ASK_TIMEOUT
            dec = None
            while True:
                try:
                    with open(_jps_os.path.join(_NET_REQ_DIR, rid + ".decision.json"),
                              "r", encoding="utf-8") as _df:
                        dec = (_jps_json.load(_df) or {}).get("decision")
                except Exception:
                    dec = None
                if dec in ("allow", "deny"):
                    break
                if _jps_time.monotonic() >= deadline:
                    break  # no answer in time -> deny (fail-closed)
                try:
                    _jps_th2.Event().wait(0.15)
                except Exception:
                    _jps_time.sleep(0.15)
            with _NET_LOCK:
                _EXEC_DECIDED[sig] = dec if dec in ("allow", "deny") else "deny"
                _EXEC_INFLIGHT.pop(sig, None)
            d = _EXEC_DECIDED[sig]
        if d == "allow":
            return  # the user allowed this exact spawn -- let it proceed
        raise PermissionError(
            "python_exec sandbox: child-process spawn NOT allowed -- %s (%s). The local "
            "user denied it or did not answer in time. Shells, interpreters and network "
            "tools are blocked from python_exec by default." % (what, str(detail)[:120]))

