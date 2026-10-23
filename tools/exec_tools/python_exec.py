"""Python execution tool -- entry point + subprocess runner.

This module is the THIN LAYER on top of the guardrail package:

    tools/exec_tools/
        _policy.py         pure security-policy data (blocklists, patterns)  -> POLICY namespace
        _ast_guard.py      AST visitor: profile import checks + hard floor   -> run_ast_check()
        _pattern_scan.py   tokenize-based string masking + regex fallback    -> run_pattern_scan()
        python_exec.py     THIS FILE: config resolution, guardrail orchestration, subprocess

SPLIT HISTORY (2026-07-14 refactor)
===================================
This used to be a ~50 KB single file ("FULLY SELF-CONTAINED" by design). The
security logic was buried in module-level functions next to the runner, which
made it hard to test and audit. It is now split into small, documented modules:

  * _policy.py      -- DATA ONLY (frozensets + regex dict), zero logic.
                       config/blocked_libs_set01.json must equal DEFAULT_FORBIDDEN_MODULES.
  * _ast_guard.py   -- the AST visitor with the two-layer model:
                         LAYER 1 profile-driven import checks (soft, per-profile set)
                         LAYER 2 hard floor (always on for non-empty sets): blocks the
                                 generic escape primitives -- attribute calls to
                                 open/exec/eval/compile/__import__ on ANY object,
                                 subscript lookups through globals()/vars(), dynamic
                                 getattr, and taint propagation from dangerous
                                 expressions. This closes the verified 2026-07-14
                                 exploit: b = globals()['__builtins__']; f = b.open;
                                 f(r'<SYSTEM_DIR>\\sensitive.txt')  # example path -- which previously passed
                                 every guardrail and read outside working_root (rc=0).
  * _pattern_scan.py-- regex defense-in-depth over tokenize-masked source, so string
                       literals can neither trigger rules nor hide payloads.

PREDICTABILITY PASS (2026-10-23)
================================
Three classes of real-world failures were reproduced and fixed here:

  1. USER SOURCE IS NO LONGER REWRITTEN. The child compiles the submitted snippet
     as-is under the stable filename "<python_exec>". This fixes:
       - decorated defs/classes (the old marker injection split "@deco" from "def"),
       - "from __future__ import ..." (the old __main__ indentation made it illegal),
       - tracebacks/SyntaxErrors that pointed at payload line ~1670 instead of the
         user's real line,
       - "__file__" raising NameError.
     Debug breadcrumbs now use sys.settrace (opt-in), not inserted code lines.
  2. LINE ENDINGS ARE NEVER TRANSLATED BY PYTHON_EXEC. The sandbox's text-mode open()
     wrapper defaults writes to newline="\\n" (no OS CRLF translation). Previously
     "open(p, 'w')" on Windows wrote CRLF, and a read-modify-write of an LF file
     converted the WHOLE file to CRLF; content that already contained CRLF became
     "\\r\\r\\n" (observed corruption, e.g. tools/web_interact/goto.py). The write_file
     tool applies the repository policy instead (LF everywhere, CRLF for .bat/.cmd).
  3. BAD-CASE RESULTS ARE DIAGNOSTIC. Internal frames are stripped, user line numbers
     are preserved, stderr is surfaced even on success, partial stdout is kept when
     code fails or times out, sandbox denials are labeled, and a user sys.exit(3) is
     no longer mistaken for a bootstrap init failure.

CLIENT DELIVERY: the CLIENT's dynamic_loader compiles this file with exec() and
cannot resolve package imports -- BUT same-package relative imports ARE handled:
tool_scanner._find_same_package_imports() detects 'from ._x import y' lines, ships
the sibling source as shared_deps, and dynamic_loader rewrites them to bare-name
assignments with the symbols injected at compile time (same mechanism web_interact
uses). Each helper module therefore imports ONLY stdlib + takes its policy data as
an argument -- no cross-imports between helpers.

ROLE PROPAGATION (Fix #4 - 2026-01-31): the SERVER ships CURRENT_USER_ROLE in
config_constants; dynamic_loader injects it into tool globals. NOTE (2026-08-28):
the guardrails below are driven by the per-profile blocked-libs SET, not by role --
nothing in this module reads the role for a security decision anymore.

PER-PROFILE BLOCKED LIBS (2026-08-20 feature, preserved exactly):
    _get_blocked_libs() resolution order:
      1. globals().get("PYTHON_EXEC_BLOCKED_LIBS") -- per-profile set shipped by the
         SERVER in tools_response and injected into tool globals by dynamic_loader
         (CLIENT sandbox path). An EMPTY list means "no restrictions at all"
         (admin clean set) and skips EVERY guardrail below -- BY DESIGN.
      2. tools.utils.get_python_exec_blocked_libs() -- legacy store kept as a safety
         net for direct callers/tests; in the live architecture no production path
         populates it (the SERVER never executes tools).
      3. DEFAULT_FORBIDDEN_MODULES -- fail-safe built-in defaults when no profile
         set is available (never fail-open).

FIX (2026-08-26): an EMPTY list skips EVERY guardrail below -- module import
         blocks, hard floor, regex layer AND the path-scope pre-exec scan. The 2026-08-23
         path-scope check used to run ALWAYS (even for the admin clean set), which blocked
         legitimate client work that passed absolute paths to subprocess even though the
         profile listed nothing to block -- contradicting this contract. Only what a
         profile explicitly lists is enforced; empty = unrestricted.

RUNTIME SANDBOX (2026-08-31): unconditional working_root containment at RUNTIME.
    Every execution prepends _sandbox_bootstrap.SANDBOX_BOOTSTRAP to the child payload:
      * a sys.addaudithook checks every open() / enumeration / native-code import
        against working_root (realpath + separator-anchored prefix -- same semantics
        as tools/path_guard.py::_is_path_inside_allowed); writes are ROOT-only, reads
        additionally allow the Python home tree so stdlib imports keep working.
      * child-process EXEC POLICY (2026-09-15): shells (cmd/powershell/sh/bash/...),
        interpreters (python/node/perl/...) and network tools (curl/wget/scp/...) can
        never be spawned; shell-context strings must contain no shell operators
        (| & ; < > ...); os.popen/os.exec* denied outright, os.fork child dies at 97.
      * socket.connect / create_connection: loopback-only by default (exfiltration
        channel closed); ADMIN opt-out via JACK_PYEXEC_NETWORK=1 in the calling process env
        (2026-09-30) -- full outbound network for admins, forwarded to the child sandbox.
      * import blocks in the child: ctypes/_ctypes/cffi plus multiprocessing/pty/
        webbrowser/venv/ensurepip (unsandboxed-child escape classes).
      * non-audited primitives (os.open flag-aware, stat/lstat/access/truncate/
        readlink, mutation ops, nt.open, sqlite3.connect) get patched with the same
        check as a second net.
      * missing/invalid JACK_PYEXEC_ROOT -> the child exits 3 BEFORE any user code
        runs (fail-closed; os._exit is C-level and cannot be intercepted).
    This is a HARD FLOOR: it runs for EVERY execution, including the EMPTY admin
    clean set that skips all STATIC guardrails by design -- filesystem containment no
    longer depends on profile configuration. The static layers above are unchanged
    and still gate imports/patterns per profile. Residuals (documented in
    _sandbox_bootstrap.py): fd-level I/O after a legal open; bare allowed external
    commands run at OS level with their own tool semantics.

CROSS-PLATFORM NOTE (2026-09-15, macOS fix): the runtime sandbox is pure CPython
    stdlib -- sys.addaudithook + builtin/module patching -- with NO platform APIs, so it
    runs identically on Windows and macOS. The 2026-08-31 revision that broke on Mac failed
    because (a) only TEMP/TMP were pinned to the sandbox tmp dir while macOS/Python tempfile
    consult TMPDIR, and (b) READ_ROOTS covered only dirname(sys.executable)/sys.prefix/
    sys.base_prefix -- a macOS Homebrew python reads stdlib from /opt/homebrew or /usr/local,
    which is NOT under those roots, so ordinary imports died. Both are fixed: TMPDIR is pinned
    here, and the bootstrap READ_ROOTS now also include every sys.path entry (see
    _sandbox_bootstrap.py).

COMPATIBILITY SHIMS: _check_forbidden_ast / _check_forbidden_patterns /
ASTSecurityVisitor are re-exported so external references to these names keep
working unchanged.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "python_exec",
        "description": "Execute Python code. Use for calculations, data processing, git commands, or any task that requires running Python code. Text-mode open() writes do not translate newlines (no CRLF conversion); use write_file for repository files that need enforced LF endings.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python code to execute. Can use import, subprocess, os, sys, json, math, re, etc."
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional timeout in seconds (default: uses config value). Useful for testing or quick checks.",
                    "default": None
                }
            },
            "required": ["code"]
        }
    }
}

import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time

# ============================================================================
# GUARDRAIL MODULE IMPORTS -- three-tier, context-proof.

# Context 1 (SERVER): real package import -- the relative form works natively.
# Context 2 (CLIENT sandbox): dynamic_loader REWRITES these lines before exec():
#   'from ._policy import POLICY'      -> 'POLICY = POLICY'   (self-assignment;
#                                          RHS resolves to the symbol step 7 of
#                                          compile_tool injected from the delivered
#                                          _policy.py shared_dep -- module-level, so no scope issue)
#   'from tools.exec_tools._x import Y' -> dead except-branch lines (never executed,
#                                          because the try-body always succeeds there).
# Context 3 (standalone file load, e.g. audit tests via spec_from_file_location):
#   relative import fails (no parent package) -> absolute 'tools.exec_tools.*' when
#   <project>/ is on sys.path -> otherwise the sibling .py files are exec'd from disk.

# NOTE: no inline comments on the import lines themselves -- dynamic_loader splits
# them on commas to build assignments; a trailing comment would corrupt a name.
# Re-exports (FORBIDDEN_FUNCTIONS / ASTSecurityVisitor) keep legacy references and
# the audit test suite working against this module.
try:
    from ._policy import POLICY, DEFAULT_FORBIDDEN_MODULES, FORBIDDEN_FUNCTIONS
    from ._ast_guard import ASTSecurityVisitor, run_ast_check, run_path_scope_check
    from ._pattern_scan import run_pattern_scan
    from ._sandbox_bootstrap import SANDBOX_BOOTSTRAP
except ImportError:
    try:
        from tools.exec_tools._policy import POLICY, DEFAULT_FORBIDDEN_MODULES, FORBIDDEN_FUNCTIONS
        from tools.exec_tools._ast_guard import ASTSecurityVisitor, run_ast_check, run_path_scope_check
        from tools.exec_tools._pattern_scan import run_pattern_scan
        from tools.exec_tools._sandbox_bootstrap import SANDBOX_BOOTSTRAP
    except ImportError:
        # Last resort: exec the sibling guardrail files from disk (audit/standalone
        # loads where neither package context exists). Never reached on SERVER or CLIENT.
        import importlib.util as _ilu_guardrail

        def _load_sibling_module(_name):
            _spec = _ilu_guardrail.spec_from_file_location(
                'tools.exec_tools.' + _name, os.path.join(os.path.dirname(__file__), _name + '.py')
            )
            _mod = _ilu_guardrail.module_from_spec(_spec)
            sys.modules[_spec.name] = _mod
            _spec.loader.exec_module(_mod)
            return _mod

        _policy_mod = _load_sibling_module('_policy')
        POLICY = _policy_mod.POLICY
        DEFAULT_FORBIDDEN_MODULES = _policy_mod.DEFAULT_FORBIDDEN_MODULES
        FORBIDDEN_FUNCTIONS = _policy_mod.FORBIDDEN_FUNCTIONS
        _ag_mod = _load_sibling_module('_ast_guard')
        ASTSecurityVisitor = _ag_mod.ASTSecurityVisitor
        run_ast_check = _ag_mod.run_ast_check
        run_path_scope_check = _ag_mod.run_path_scope_check
        run_pattern_scan = _load_sibling_module('_pattern_scan').run_pattern_scan
        SANDBOX_BOOTSTRAP = _load_sibling_module('_sandbox_bootstrap').SANDBOX_BOOTSTRAP

logger = logging.getLogger("COOLEMS.Tools.Exec")


# ============================================================================
# OUT-OF-SANDBOX NETWORK CONSENT RELAY (2026-11-24)
# ----------------------------------------------------------------------------
# The child sandbox's socket gate (_sandbox_bootstrap.py, stage B7) no longer just
# hard-blocks a non-loopback connect: when the parent tells it a UI relay is live
# (JACK_PYEXEC_NET_CONSENT=1 in the child env), it writes a request file to
# <working_root>/.temp/.jack_netreq/<id>.json and waits for a matching
# <id>.decision.json. This section is that relay: while the child runs, we scan the
# folder (in _run_sandboxed_subprocess's poll loop) and forward every request to the
# chat UI as an approval card -- the SAME mechanism as python_exec / URL-consent cards
# (exec_approval_request frame -> POST /api/exec-approval -> ExecApprovalState).
#
# Context bridge: RemoteToolOrchestrator.execute_tool wraps every python_exec call in
# _call_pyexec_with_context(), which stores {'websocket', 'conversation_id', 'loop'} on
# a process-wide anchor module (sys.modules[_PYEXEC_CTX_KEY]) for exactly as long as
# THIS call runs -- set and restored inside the worker thread that executes the tool,
# so concurrent chats never see each other's context. Non-CLIENT callers (direct
# tests / SERVER) have no anchor entry -> callback is None -> the sandbox keeps its
# original instant hard block (JACK_PYEXEC_NET_CONSENT=0). Fail-closed everywhere:
# any relay error or a missing answer denies the connection.
# ============================================================================

_PYEXEC_CTX_KEY = "_coolems_pyexec_context"


def _get_caller_context():
    """Read the per-tool-call context set by RemoteToolOrchestrator.execute_tool.

    Returns the {'websocket', 'conversation_id', 'loop'} dict for THIS python_exec
    call, or None when no relay context exists (non-CLIENT callers / direct tests).
    """
    m = sys.modules.get(_PYEXEC_CTX_KEY)
    local = getattr(m, "context_local", None) if m is not None else None
    # threading.local on purpose: each python_exec runs in its own asyncio.to_thread
    # worker thread, so concurrent chats never read each other's context.
    ctx = getattr(local, "context", None) if local is not None else None
    return ctx if isinstance(ctx, dict) else None


# ============================================================================
# EXPLAIN REQUEST STASH (2026-07-18 unified consent dialog). When the user clicks
# "Explain request" on a file/network consent card raised from INSIDE this
# python_exec call, the relay denies that one operation (fail-closed) and stashes
# the AI-facing instruction here; drain_explain_instructions() appends it to this
# tool call's result so the ReAct loop sees it in THIS turn. Thread-safe: the relay
# runs on daemon threads while the main thread drains after the child exits.
# ============================================================================
import threading as _threading

_EXPLAIN_STASH = []          # list[str] - pending explain instructions for this run
_EXPLAIN_LOCK = _threading.Lock()


def stash_explain_instruction(text):
    """Relay-side: remember an Explain-request instruction to surface in the result."""
    with _EXPLAIN_LOCK:
        _EXPLAIN_STASH.append(str(text))


def drain_explain_instructions():
    """Main-thread side: pop ALL stashed instructions, one joined block (or empty string)."""
    with _EXPLAIN_LOCK:
        items = list(_EXPLAIN_STASH)
        del _EXPLAIN_STASH[:]
    if not items:
        return ""
    return "\n\n" + "\n\n".join(items)

def _make_net_ask_callback(ctx, working_dir):
    """Build the sync request handler used by _run_sandboxed_subprocess's poller.

    Returns a function req_dict -> None that spawns ONE daemon thread per network
    request: it delivers an 'exec_approval_request' frame (kind='network') to the chat
    UI, awaits the user's decision through the channel's ExecApprovalState (identical
    plumbing to python_exec / URL-consent cards), and writes the child's
    <id>.decision.json file with 'allow' or 'deny'. Every failure path fails closed ->
    'deny'. Returns None when no usable relay context exists -- the sandbox then keeps
    its original instant hard block.
    """
    ws = ctx.get("websocket") if isinstance(ctx, dict) else None
    conv_id = ctx.get("conversation_id") if isinstance(ctx, dict) else None
    loop = ctx.get("loop") if isinstance(ctx, dict) else None
    if ws is None or not conv_id or loop is None:
        return None

    import asyncio as _aio

    async def _ask_coro(req):
        """Runs on the uvicorn loop (via run_coroutine_threadsafe). Always returns
        'allow' or 'deny'; never raises."""
        try:
            from app.chat_bus import get_chat_bus, build_frame, explain_instruction
            bus = get_chat_bus()
            channel = bus.channels.get(conv_id) if bus is not None else None
            state = getattr(channel, "exec_approval", None) if channel is not None else None
            if state is None or not hasattr(state, "wait_for_decision"):
                return "deny"  # fail closed: no live approval state for this chat
            kind = str(req.get("kind", "")).strip() or "network"  # 'file' | 'network' | 'subprocess' (2026-10-24)
            # (2026-07-18 rev4) per-kind grant: an earlier card of THIS SAME KIND granted
            # "Allow until task done" this run - allow without asking. Other kinds still ask.
            if getattr(state, "is_allowed_till_task_done", lambda k: False)(kind):
                return "allow"
            if kind == "subprocess":
                # OUT-OF-SANDBOX SUBPROCESS CONSENT (2026-10-24): the approved script wants to
                # SPAWN a shell / interpreter / network tool that the sandbox denies by default.
                what = str(req.get("what", "?"))
                command = str(req.get("command", "?"))
                payload = ("# attempted by the approved python_exec code\n" + command)
                description = ("The Python code you approved is trying to RUN AN EXTERNAL PROGRAM "
                               "that the python_exec sandbox normally blocks: %s. Command: %s. "
                               "Allow runs exactly this one spawn (each different command asks "
                               "again); Deny keeps it blocked. NOTE: an allowed program runs at OS "
                               "level, outside the sandbox's file containment." % (what, command))
            elif kind == "file":
                # OUT-OF-SANDBOX FILE CONSENT (2026-11-24): the approved script wants to
                # touch a path outside working_root. Show the exact operation + path.
                op = str(req.get("op", "?"))
                fpath = str(req.get("path", "?"))
                payload = ("# attempted by the approved python_exec code (operation: %s)" % op)
                description = ("The Python code you approved is trying to ACCESS A FILE "
                               "OUTSIDE your working_root -- the python_exec sandbox normally "
                               "blocks this. Operation: %s on path: %s. Allow opens exactly "
                               "this path (and its folder subtree) for the rest of this run; "
                               "Deny keeps it blocked." % (op, fpath))
            else:
                host = str(req.get("host", "?"))
                port = req.get("port", "")
                method = str(req.get("method", "connect"))
                payload = ("socket.%s((%r, %s))   # attempted by the approved python_exec code"
                           % (method, host, port))
                description = ("The Python code you approved is trying to connect OUTSIDE "
                               "your machine -- the python_exec sandbox normally blocks this. "
                               "Target: %s:%s via socket.%s(). Allow opens exactly this one "
                               "connection for the rest of this run; Deny keeps it blocked."
                               % (host, port, method))
            rid = state.new_request_id()
            await ws.send_text(json.dumps(build_frame(
                conv_id, rid, payload, kind=kind, description=description)))
            decision = await state.wait_for_decision(rid, kind=kind)
            if decision == "explain":
                # (2026-07-18 unified dialog) the user wants an explanation: deny THIS one
                # operation (fail-closed - asking for info allows nothing) and stash the
                # instruction so drain_explain_instructions() surfaces it in this tool call's
                # result. The AI then explains + re-issues python_exec -> a fresh card appears.
                stash_explain_instruction(explain_instruction(kind, payload[:200]))
                return "deny"
            return "allow" if decision in ("allow", "allow_category_until_done") else "deny"
        except BaseException:
            # Covers asyncio.CancelledError (BaseException since py3.8): the turn was
            # stopped/superseded while we waited -- fail closed with an immediate deny so
            # the child does not sit out its full ask timeout for nothing.
            return "deny"  # fail closed on any relay error or cancellation

    def _handle_request(req):
        """Daemon-thread body: ask the UI, then write the child's decision file."""
        rid = str(req.get("request_id", "")) if isinstance(req, dict) else ""
        req_dir = os.path.join(working_dir, ".temp", ".jack_netreq")
        decision = "deny"
        try:
            fut = _aio.run_coroutine_threadsafe(_ask_coro(req), loop)
            # Hard cap slightly above the child's own ask timeout (<=600s): if the turn
            # dies without resolving the future we still deny instead of leaking a task.
            decision = str(fut.result(timeout=610.0))
        except BaseException:
            # BaseException on purpose: asyncio.CancelledError / loop death must also
            # fail closed -- an uncaught exception here would skip the decision file and
            # make the child wait out its full ask timeout before denying (audit 2026-11-24).
            decision = "deny"  # fail closed
        if rid and decision in ("allow", "deny"):
            try:
                dec_path = os.path.join(req_dir, rid + ".decision.json")
                with open(dec_path, "w", encoding="utf-8") as f:
                    json.dump({"request_id": rid, "decision": decision}, f)
            except Exception:
                pass  # child will time out and deny on its own

    return _handle_request
    

# ============================================================================
# DEBUG MARKERS (2026-09-16): numbered checkpoints for hang diagnosis.
# OPT-IN ONLY since 2026-09-30 (JACK_PYEXEC_DEBUG=1 in the calling process env -- client-console
# cleanup: default runs are fully silent). When enabled, every python_exec call prints
# [PYEXEC] #N lines to stderr AND appends them to
# <working_root>/.temp/.jack_pyexec_debug.log -- so even when the tool call never
# returns, the LAST printed number tells exactly which stage it stopped in:
#   parent (this process): 1 entry -> 2 params ok -> 3 working_root resolved
#                          -> 4 blocked-libs resolved -> 5 path-scope check
#                          -> 6 security checks done -> 7 about to spawn child
#                          -> 8 spawned/waiting (+ #8.x heartbeat every 10s while the
#                             child is still running) -> 9 child exited (rc + secs)
#                          -> 10 output captured -> 11 returning result
#   child process:         C1 entry ran (payload read from stdin, fd 0 closed so spawned
#                          tools get EOF instead of a live pipe) -> C2 sandbox bootstrap
#                          installed -> C3 user source compiled -> C4 per-line tracer
#                          (opt-in; reports "user line N" via sys.settrace, no source
#                          rewriting) -> C5 user code finished.
#   Child markers ALSO append to <working_root>/.temp/.jack_pyexec_child.log from the child
#   itself, so they survive even when the parent captures stderr into a pipe and never
#   returns it -- that is what made Mac hangs at #7 blind. After a hang, read that file:
#   its LAST line says exactly where the child stopped (C1 = bootstrap stage, C4 line N =
#   your line N). ALL [PYEXEC] marker output (parent stderr + both .temp/ log files) is
#   OFF by default (2026-09-18 file cleanup, 2026-09-30 stderr/client-console cleanup); set env
#   JACK_PYEXEC_DEBUG=1 to opt back in.
# ============================================================================

def _pyexec_dbg(msg, working_dir=None):
    """Emit a numbered debug marker -- OPT-IN ONLY (2026-09-30 client-console cleanup).

    OFF by default: normal runs print NOTHING to stderr and write no log file, keeping
    the client console clean (the always-on [PYEXEC] #N stderr markers were needed while
    fixing hang bugs but are pure noise now -- user request 2026-09-30). Set env
    JACK_PYEXEC_DEBUG=1 in the CALLING process to opt back in: then every marker prints
    to stderr AND appends to <working_root>/.temp/.jack_pyexec_debug.log (the LAST line =
    where a hang stopped). The flag is forwarded to the child, so one switch re-enables
    parent #N markers and child C*/B* file logs together."""
    if os.environ.get("JACK_PYEXEC_DEBUG", "0") == "0":
        return  # default: fully silent -- no stderr clutter, no log files (2026-09-30)
    import time as _t_dbg
    line = "[PYEXEC] %s | t=%.3f" % (msg, _t_dbg.time())
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    if working_dir:
        try:
            _logdir = os.path.join(working_dir, ".temp")  # user policy (2026-09-18): python_exec artifacts live in .temp/, not the working_root top level
            os.makedirs(_logdir, exist_ok=True)
            with open(os.path.join(_logdir, ".jack_pyexec_debug.log"), "a", encoding="utf-8") as _f_dbg:
                _f_dbg.write(line + "\n")
        except Exception:
            pass  # logging must never break execution


# Child-side debug preamble + entry point (2026-09-17, macOS hang fix).
# Built from line LISTS joined with chr(10) -- no backslash escapes anywhere in this
# source, so the delivered strings are byte-exact on every platform/loader.
#
# All [PYEXEC] marker output below (child C* stderr + .jack_pyexec_child.log) is OPT-IN ONLY
# since 2026-09-30 (JACK_PYEXEC_DEBUG=1, client-console cleanup): default runs are fully silent.
# The parent captures the child's stderr into a pipe: anything printed there is
# invisible in the client console and only reaches us IF the run returns. When the
# child hangs (the Mac case) that output never comes back -- the log stopped at #7
# with zero child-side visibility. _jpsd_log() therefore appends every C* marker BY
# THE CHILD to <working_root>/.temp/.jack_pyexec_child.log: readable even when the run
# never returns. Its LAST line says exactly where the child stopped (C1 = bootstrap,
# C4 = the exact line of YOUR code that was executing; opt-in sys.settrace hook).

_CHILD_PREAMBLE_LINES = [
    "import sys as _jps_sys, os as _jps_os",
    "_JPSD_ROOT = _jps_os.environ.get('JACK_PYEXEC_ROOT') or ''",
    "def _jpsd_log(msg):",
    "    try:",
    "        if _jps_os.environ.get('JACK_PYEXEC_DEBUG', '0') != '0':  # OPT-IN ONLY (2026-09-30 client-console cleanup)",
    "            print('[PYEXEC] ' + msg, file=_jps_sys.stderr, flush=True)",
    "    except Exception:",
    "        pass",
    "    if _JPSD_ROOT and _jps_os.environ.get('JACK_PYEXEC_DEBUG', '0') != '0':",
    "        try:",
    "            _ld = _jps_os.path.join(_JPSD_ROOT, '.temp')",
    "            _jps_os.makedirs(_ld, exist_ok=True)",
    "            with open(_jps_os.path.join(_ld, '.jack_pyexec_child.log'), 'a', encoding='utf-8') as _f:",
    "                _f.write('[PYEXEC] ' + msg + chr(10))",
    "        except Exception:",
    "            pass",
]
_CHILD_PREAMBLE = chr(10).join(_CHILD_PREAMBLE_LINES) + chr(10)

# Child entry point: the -c command itself. It reads the payload from stdin and CLOSES
# fd 0 in the child before exec'ing it. The parent keeps the write end of that pipe
# open until the child exits; if the child left stdin open, every tool it spawns (git
# & co.) would INHERIT a live stdin pipe instead of EOF -- any git prompt/pager/
# credential read then blocks forever waiting for input that will never come. Closing
# fd 0 hands spawned tools an immediate EOF: prompts get no input and exit with their
# own error code instead of hanging the whole run. (User code reading sys.stdin now
# gets a ValueError -- by design, it cannot hang.)
_CHILD_ENTRY_LINES = [
    "import sys as _jps_sys, os as _jps_os",
    "_JPSD_PAYLOAD = _jps_sys.stdin.read()",
    "try:",
    "    _jps_sys.stdin.close()",
    "except Exception:",
    "    pass",
    "def _jpsd_c1():",
    "    m = 'C1: child started, payload read (%d chars), stdin closed for spawned tools' % len(_JPSD_PAYLOAD)",
    "    try:",
    "        if _jps_os.environ.get('JACK_PYEXEC_DEBUG', '0') != '0':  # OPT-IN ONLY (2026-09-30 client-console cleanup)",
    "            print('[PYEXEC] ' + m, file=_jps_sys.stderr, flush=True)",
    "    except Exception:",
    "        pass",
    "    _r = _jps_os.environ.get('JACK_PYEXEC_ROOT') or ''",
    "    if _r and _jps_os.environ.get('JACK_PYEXEC_DEBUG', '0') != '0':",
    "        try:",
    "            _ld = _jps_os.path.join(_r, '.temp')",
    "            _jps_os.makedirs(_ld, exist_ok=True)",
    "            with open(_jps_os.path.join(_ld, '.jack_pyexec_child.log'), 'a', encoding='utf-8') as _f:",
    "                _f.write('[PYEXEC] ' + m + chr(10))",
    "        except Exception:",
    "            pass",
    "_jpsd_c1()",
    "exec(compile(_JPSD_PAYLOAD, '<python_exec_payload>', 'exec'))",
]
_CHILD_ENTRY = chr(10).join(_CHILD_ENTRY_LINES) + chr(10)



# ============================================================================
# CONFIG RESOLVER -- single source of truth is tools/utils.py.
# CLIENT sandbox: dynamic_loader injects utils attributes into globals().
# ============================================================================

def _cfg(name, default):
    """Resolve a config constant from utils module or globals.

    Resolution order:
      1. globals().get(name) -- CLIENT sandbox injection by dynamic_loader
      2. sys.modules['tools.utils'].name -- SERVER normal import / CLIENT shared_utils
      3. default -- ultimate fallback (should never be reached in practice)
    """
    v = globals().get(name)
    if v is not None:
        return v
    try:
        _u = sys.modules.get('tools.utils')
        if _u is not None:
            val = getattr(_u, name, None)
            if val is not None:
                return val
    except Exception:
        pass
    return default


# Config constants -- single source of truth is tools/utils.py
FILE_EXEC_OUTPUT_TRUNCATE_CHARS = _cfg("FILE_EXEC_OUTPUT_TRUNCATE_CHARS", 10000)
FILE_EXEC_ERROR_TRUNCATE_CHARS = _cfg("FILE_EXEC_ERROR_TRUNCATE_CHARS", 2000)
FILE_EXEC_TIMEOUT = _cfg("FILE_EXEC_TIMEOUT", 300)


# ============================================================================
# SECURITY CONFIG RESOLUTION (role + per-profile blocked-libs set)
# ============================================================================

def _get_blocked_libs():
    """Resolve the ACTIVE python_exec module blocklist for this execution.

    Resolution order:
      1. globals().get("PYTHON_EXEC_BLOCKED_LIBS") -- per-profile set shipped by the
         SERVER in tools_response and injected into tool globals by dynamic_loader
         (CLIENT sandbox path). An EMPTY list here means "no restrictions at all"
         (admin clean set) and skips EVERY guardrail below.
      2. tools.utils.get_python_exec_blocked_libs() -- legacy store kept as a safety
         net for direct callers/tests; in the live architecture no production path
         populates it (the SERVER never executes tools).
      3. DEFAULT_FORBIDDEN_MODULES -- fail-safe built-in defaults when no profile
         set is available (never fail-open).

    Returns:
        frozenset of module names to block on import, or an EMPTY frozenset()
        meaning "allow everything" (admin clean set).
    """
    injected = globals().get("PYTHON_EXEC_BLOCKED_LIBS")
    if isinstance(injected, list):
        return frozenset(m for m in injected if isinstance(m, str) and m.strip())
    try:
        _u = sys.modules.get('tools.utils')
        if _u is not None and hasattr(_u, 'get_python_exec_blocked_libs'):
            stored = _u.get_python_exec_blocked_libs()
            if stored is not None:
                return frozenset(m for m in stored if isinstance(m, str) and m.strip())
    except Exception:
        pass
    return DEFAULT_FORBIDDEN_MODULES


def _path_scope_enabled():
    """Whether the path-scope guardrail is active for this execution.

    Gated by the per-profile blocked-libs set alone (see python_exec()): it runs
    only when the active profile set is NON-EMPTY; an EMPTY admin clean set skips
    every guardrail BY DESIGN, including this one.

    NOTE (2026-08-28): a former second resolution tier read the flag from
    tools.utils.get_python_exec_path_scope_guard() -- that store was broken
    (NameError on every call) and never wired to any profile key or injection, so
    it was removed along with its utils.py functions. There is no individual flag
    for this guardrail; only the per-profile blocked-libs set governs it.
    """
    return True


def _get_working_dir():
    """Resolve working directory -- MUST be a configured working_root.

    SECURITY FIX (2026-07-15): previously fell back to ``os.getcwd()`` when the
    working_root could not be resolved, which let executed code operate in an
    arbitrary directory OUTSIDE the working_root lock. It now raises instead so
    we fail loud and never silently escape the tree.

    Resolution order:
      1. globals()['get_working_root']          -- CLIENT sandbox injection by dynamic_loader
      2. sys.modules['tools.utils'].get_working_root -- SERVER normal import / installed shared module
      3. raise RuntimeError (NO CWD fallback)
    """
    gwr = globals().get("get_working_root")
    if callable(gwr):
        return gwr()
    try:
        _u = sys.modules.get('tools.utils')
        if _u is not None and hasattr(_u, 'get_working_root'):
            return _u.get_working_root()
    except Exception:
        pass
    raise RuntimeError(
        "working_root not configured for python_exec. Refusing to fall back to "
        "the process CWD -- all execution must stay inside working_root."
    )


# NOTE (2026-08-28): the former _get_current_user_role() resolver was removed. Its
# result was assigned to `current_role` in python_exec() and never used -- every
# guardrail here is driven by the per-profile blocked-libs set, not by role. The
# SERVER still ships CURRENT_USER_ROLE in config_constants (harmless identity data
# for other tools); this module simply does not consume it.


# ============================================================================
# SECURITY CHECK ORCHESTRATION -- runs AST + regex, deduplicates violations.
# The heavy lifting lives in _ast_guard.run_ast_check / _pattern_scan.run_pattern_scan;
# the wrappers below keep the legacy call signatures (audit-suite compatible).
# ============================================================================

def _check_forbidden_ast(code):
    """Parse code into AST and detect dangerous patterns (PRIMARY check).

    Delegates to _ast_guard.run_ast_check with the ACTIVE profile blocklist.
    Returns (has_violations, violations) -- legacy contract preserved.
    """
    return run_ast_check(code, _get_blocked_libs(), POLICY)


def _check_forbidden_patterns(code):
    """Scan code for forbidden patterns (SECONDARY regex fallback).

    Delegates to _pattern_scan.run_pattern_scan over tokenize-masked source.
    Returns (has_violations, violations) -- legacy contract preserved.
    """
    return run_pattern_scan(code, POLICY)


def _run_security_checks(code):
    """Run ALL security checks on the code.

    Uses AST parsing as PRIMARY check (catches obfuscated attempts), then runs
    the regex layer over string-masked source for anything AST misses.
    Deduplicates violations by message content.
    """
    all_violations = []

    ast_has_issues, ast_violations = _check_forbidden_ast(code)
    if ast_has_issues:
        all_violations.extend(ast_violations)

    regex_has_issues, regex_violations = _check_forbidden_patterns(code)
    if regex_has_issues:
        all_violations.extend(regex_violations)

    # Deduplicate by message content (keep first occurrence)
    seen = set()
    unique_violations = []
    for v in all_violations:
        if v not in seen:
            seen.add(v)
            unique_violations.append(v)

    return len(unique_violations) > 0, unique_violations


# ============================================================================
# UTILITY HELPERS
# ============================================================================

def _safe_strip(value):
    """Safely strip a string value."""
    return value.strip() if value else ""



def _to_lf(text, lone_cr=False):
    """Normalize a tool-facing string to LF.

    Child stdout/stderr are TextIOWrapper streams on Windows, so Python translates
    every ``\\n`` to ``\\r\\n`` before it reaches our binary pipe. Returning CRLF to
    the model made tool output look inconsistent with file content and sometimes
    leaked into generated files. ``lone_cr`` additionally maps a bare CR to LF (used
    for diagnostics only; stdout keeps a lone CR so progress barriers survive).
    """
    if not isinstance(text, str) or not text:
        return ""
    text = text.replace("\r\n", "\n")
    if lone_cr:
        text = text.replace("\r", "\n")
    return text


def _clean_output(text):
    """Normalize stdout to LF and drop only TRAILING newlines.

    Leading whitespace is significant for tables/JSON/CSV, so this deliberately
    does not use .strip() (the old code silently ate the first indent of output).
    """
    return _to_lf(text).rstrip("\n")


def _truncate(text, limit, label="truncated"):
    """Truncate *text* to *limit* chars, stating exactly how much was shown."""
    if text is None:
        return ""
    if limit is None or len(text) <= limit:
        return text
    return (text[:limit]
            + "\n\n...[%s: showing first %d of %d chars]..." % (label, limit, len(text)))


def _truncate_tail(text, limit, label="truncated"):
    """Truncate while keeping the END of *text* -- tracebacks put the exception last.

    Keeping only the first N chars used to cut off the actual error line of a long
    traceback, which is the one line an AI needs. This keeps a small head plus the
    tail (where the exception is).
    """
    if text is None:
        return ""
    if limit is None or len(text) <= limit:
        return text
    head = max(200, limit // 4)
    tail = max(400, limit - head)
    omitted = len(text) - head - tail
    if omitted <= 0:
        return text[:limit]
    return (text[:head]
            + "\n\n...[%s: %d middle chars omitted]...\n\n" % (label, omitted)
            + text[-tail:])


def _decode_part(value):
    """Decode a TimeoutExpired output field that may be str or bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# Frames that belong to the python_exec infrastructure, not to the user's code.
# The child entry runs under "<string>" (python -c) and the payload under
# "<python_exec_payload>"; neither helps an AI fix the user's snippet.
def _clean_child_stderr(text):
    """Return child stderr with python_exec's own traceback frames removed.

    The user's frames are compiled as ``<python_exec>`` and keep their exact source
    line numbers. We drop only the FIRST ``<string>`` frame (the ``python -c`` entry)
    so a user snippet that deliberately uses ``compile(..., "<string>", ...)`` keeps
    its own frames.
    """
    text = _to_lf(text, lone_cr=True)
    if not text:
        return ""
    kept = []
    string_frames_seen = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith('File "<python_exec_payload>"'):
            continue
        if stripped.startswith('File "<string>"'):
            string_frames_seen += 1
            if string_frames_seen == 1:
                continue
        kept.append(line)
    # Collapse runs of blank lines produced by removing frames.
    out = []
    blank = 0
    for line in kept:
        if line.strip() == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip()


def _user_lineno(text):
    """Extract the LAST ``<python_exec>`` line number from a traceback, or None."""
    if not text:
        return None
    matches = re.findall(r'File "<python_exec>", line (\d+)', text)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except Exception:
        return None


def _source_line(code, lineno):
    """Return the user's source line *lineno* (1-based), stripped, or ''."""
    if not code or not lineno:
        return ""
    try:
        lines = code.splitlines()
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1].strip()
    except Exception:
        pass
    return ""


def _last_nonempty_line(text):
    for line in reversed((text or "").split("\n")):
        if line.strip():
            return line.strip()
    return ""


def _coerce_timeout(timeout):
    """Validate/coerce the tool's timeout argument.

    Returns (seconds, None) on success or (None, "Error: ...") on bad input. Doing
    this BEFORE spawning turns a TypeError deep in the poll loop into a clear,
    actionable message.
    """
    raw = FILE_EXEC_TIMEOUT if timeout is None else timeout
    try:
        if isinstance(raw, bool):
            raise TypeError("bool")
        value = float(raw)
    except (TypeError, ValueError):
        return None, ("Error: timeout must be a number of seconds "
                      "(for example 30). Got: %r" % (timeout,))
    if not math.isfinite(value) or value <= 0:
        return None, "Error: timeout must be a positive number of seconds."
    if value > 86400:
        return None, "Error: timeout is too large (maximum 86400 seconds = 24 hours)."
    return value, None


def _format_success(stdout, stderr):
    """Render a successful run, surfacing non-fatal stderr instead of dropping it."""
    out = _clean_output(stdout)
    err = _clean_child_stderr(stderr)
    if not out.strip() and not err:
        return "Executed successfully (no output)"
    parts = []
    if out.strip():
        parts.append(_truncate(out, FILE_EXEC_OUTPUT_TRUNCATE_CHARS, "output truncated"))
    else:
        parts.append("Executed successfully (no output on stdout)")
    if err:
        # Warnings, logging and progress written to stderr used to be discarded on
        # rc=0. They are often the ONLY clue an AI needs to fix a subtle bug.
        parts.append("--- stderr (warnings/logs; execution succeeded) ---\n"
                     + _truncate_tail(err, FILE_EXEC_ERROR_TRUNCATE_CHARS, "stderr truncated"))
    return "\n\n".join(parts)


def _format_failure(code, returncode, stdout, stderr):
    """Render a failed run with a clean, AI-actionable diagnosis.

    Contract: every bad-case result starts with a one-line category, then the user's
    source line (when known), then the cleaned traceback and any stdout emitted
    before the failure. Nothing points at python_exec's internal payload lines.
    """
    out = _clean_output(stdout)
    err = _clean_child_stderr(stderr)
    detail = err or out
    lineno = _user_lineno(err) or _user_lineno(out)
    exc_line = _last_nonempty_line(err) or _last_nonempty_line(out)

    # Fail-closed bootstrap init: the child exits 3 BEFORE user code runs. Do not
    # confuse this with user code calling sys.exit(3) -- require the bootstrap's
    # exact failure marker text.
    if returncode == 3 and "python_exec sandbox INIT FAILED" in detail:
        return (
            "EXECUTION BLOCKED by python_exec runtime sandbox (fail-closed init).\n\n"
            + _truncate_tail(detail, FILE_EXEC_ERROR_TRUNCATE_CHARS, "detail truncated")
            + "\n\nThe child process could not verify the working_root lock and was "
              "terminated before any code ran. Configure working_root first."
        )

    if returncode is not None and returncode < 0:
        head = ("Error: the Python child process was killed by signal %d "
                "(the host or the sandbox terminated it)." % (-returncode))
    elif "SyntaxError" in err or "IndentationError" in err or "TabError" in err:
        head = "Error: your Python code has a syntax error"
        if lineno:
            head += " at line %d" % lineno
        head += "."
    elif "python_exec sandbox" in err:
        head = ("SANDBOX BLOCKED: the python_exec runtime sandbox denied an operation. "
                "This is a containment/policy block, not a syntax error.")
    elif "ModuleNotFoundError" in err:
        m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", err)
        if m:
            head = ("Error: Python module '%s' is not available in the python_exec "
                    "environment. Install it or use the standard library." % m.group(1))
        else:
            head = "Error: a required Python module is not available in this environment."
    elif "MemoryError" in err:
        head = "Error: the code ran out of memory inside python_exec."
    elif "RecursionError" in err:
        head = "Error: maximum recursion depth exceeded."
    elif returncode == 3:
        head = ("Error: the Python process exited with code 3 (user code called "
                "exit(3) or SystemExit(3)); this is NOT a sandbox init failure.")
    elif not err:
        head = ("Error: the Python process exited with code %s without writing to "
                "stderr (user code may have called os._exit() or the interpreter was "
                "terminated)." % returncode)
    else:
        head = "Error: your Python code failed%s." % ((" (%s)" % exc_line) if exc_line else "")

    parts = [head]
    if lineno:
        src = _source_line(code, lineno)
        if src:
            parts.append("Your code line %d: %s" % (lineno, src))
        else:
            parts.append("Failing user line: %d" % lineno)
    if err:
        parts.append("--- traceback (line numbers refer to YOUR code) ---\n"
                     + _truncate_tail(err, FILE_EXEC_ERROR_TRUNCATE_CHARS, "traceback truncated"))
    elif out and not exc_line:
        parts.append("(the child produced no stderr)")
    if out:
        parts.append("--- stdout before the failure ---\n"
                     + _truncate(out, FILE_EXEC_OUTPUT_TRUNCATE_CHARS, "output truncated"))
    return "\n\n".join(parts)


def _format_timeout(exc, effective_timeout):
    """Render a timeout with partial stdout/stderr and a concrete next step."""
    out = _clean_output(_decode_part(getattr(exc, "stdout", None)))
    err = _clean_child_stderr(_decode_part(getattr(exc, "stderr", None)))
    parts = [
        "EXECUTION TIMEOUT: code execution exceeded %g second%s."
        % (effective_timeout, "" if effective_timeout == 1 else "s"),
        "The child process (and its whole process tree on POSIX) was terminated.",
    ]
    if out:
        parts.append("--- partial stdout (before timeout) ---\n"
                     + _truncate(out, FILE_EXEC_OUTPUT_TRUNCATE_CHARS, "output truncated"))
    if err:
        parts.append("--- partial stderr (before timeout) ---\n"
                     + _truncate_tail(err, FILE_EXEC_ERROR_TRUNCATE_CHARS, "stderr truncated"))
    parts.append(
        "How to fix it:\n"
        "  - Reduce the work per call (smaller input, fewer iterations) and split the task.\n"
        "  - Remove blocking waits: stdin is CLOSED in python_exec (input() raises), and\n"
        "    non-loopback network access needs explicit user consent.\n"
        "  - Add progress prints/flush so the next failure shows how far it got.\n"
        "  - Raise the 'timeout' argument only when the work is genuinely long."
    )
    return "\n\n".join(parts)


def _wrap_user_code(user_code):
    """Build the child-side exec block for *user_code* (2026-10-23 rewrite).

    WHY THIS IS NO LONGER A TEXT WRAPPER
    ====================================
    The old implementation physically inserted ``_jpsd_log(...)`` lines between
    top-level statements and indented everything under ``if __name__ == "__main__":``.
    That looked harmless but produced three verified classes of failures:

      1. DECORATOR SPLIT: a decorated ``def``/``class`` has ``node.lineno`` on the
         ``def``/``class`` keyword, NOT on the ``@decorator`` line. The marker was
         inserted between them, so ANY decorated function/class died with
         ``SyntaxError: invalid syntax`` -- and decorators are everywhere
         (``@dataclass``, ``@property``, ``@pytest.fixture``, Flask routes, ...).
      2. FUTURE IMPORTS: ``from __future__ import annotations`` must be the first
         statement in the file. Indented inside the ``if`` block it raised
         ``SyntaxError: from __future__ imports must occur at the beginning``.
      3. USELESS LINE NUMBERS: markers shifted user lines, so tracebacks pointed at
         ``<python_exec_payload>`` line ~1670 instead of the user's line 3.

    The fix is to stop rewriting the user's source entirely. The child compiles the
    ORIGINAL source with a stable filename (``<python_exec>``) and executes it at
    module scope. This:
      * preserves exact user line numbers in SyntaxError/traceback output;
      * keeps decorators, future imports, multi-line statements and comments intact;
      * makes ``__file__`` deterministic instead of raising NameError;
      * allows debug breadcrumbs via ``sys.settrace`` (only when JACK_PYEXEC_DEBUG=1),
        so the C4 line markers no longer mutate the code at all.

    SECURITY NOTE: the old ``if __name__ == "__main__":`` wrapper was believed to be
    what makes ``globals()['__builtins__']`` a builtins MODULE in the child. That is
    actually a property of ``python -c`` script context, not of the guard: the child
    entry already runs as ``__main__`` and ``exec`` shares those globals, so the
    guardrail model in _ast_guard.py is unchanged. The full escape battery is rerun
    by the regression tests in tools/test_tools/test_python_exec_regressions.py.
    """
    return "\n".join([
        "_USER_FILENAME = '<python_exec>'",
        "_USER_SOURCE = " + repr(user_code),
        # Deterministic __file__: snippets that do Path(__file__).parent or log the
        # script name no longer die with NameError. It is a label, not a real path.
        "__file__ = _USER_FILENAME",
        # argv[0] used to be "-c" (the interpreter flag). Give it the same stable
        # label so argparse-style code does not show a confusing program name.
        "_jps_sys.argv = ['<python_exec>']",
        "_jpsd_log('C3: compiling user code (%d chars)' % len(_USER_SOURCE))",
        "try:",
        "    _USER_CODE = compile(_USER_SOURCE, _USER_FILENAME, 'exec')",
        "except SyntaxError:",
        "    raise",
        # C4 breadcrumbs: opt-in line tracing on the ORIGINAL source. No source
        # mutation, no line offsets, no decorator hazard. Only when debug is on.
        "if _jps_os.environ.get('JACK_PYEXEC_DEBUG', '0') != '0':",
        "    def _jpsd_trace(frame, event, arg):",
        "        try:",
        "            if event == 'line' and frame.f_code.co_filename == _USER_FILENAME:",
        "                _jpsd_log('C4: child executing user line %d' % frame.f_lineno)",
        "        except Exception:",
        "            pass",
        "        return _jpsd_trace",
        "    _jps_sys.settrace(_jpsd_trace)",
        "try:",
        "    exec(_USER_CODE, globals())",
        "finally:",
        "    try:",
        "        _jps_sys.settrace(None)",
        "    except Exception:",
        "        pass",
        "_jpsd_log('C5: child user code finished')",
    ])


def _run_sandboxed_subprocess(wrapped_code, working_dir, effective_timeout,
                              net_ask_callback=None):
    """Run a sandbox-prefixed payload in an isolated child process.

    * Prepends SANDBOX_BOOTSTRAP (the runtime containment layer) to the wrapped
      user code -- UNCONDITIONAL: every execution gets it, admin clean set included.
    * Exposes the lock via JACK_PYEXEC_ROOT; the bootstrap exits 3 fail-closed if
      that env var is missing or invalid (before any user code runs).
    * Pins TEMP/TMP/TMPDIR to <working_root>/.temp so every temp file created by an
      execution lands in one dedicated folder (never scattered across working_root).
    * Delivers the payload via STDIN (`python -c "<entry>"`) instead of `python -c <payload>`:
      Windows caps the command line at 32,768 chars and the bootstrap alone is ~23 KB --
      stdin delivery removes that limit for user code (up to the 1 MB tool cap) on every
      platform. The entry (_CHILD_ENTRY) reads the payload from stdin and CLOSES fd 0 in
      the child first, so spawned tools inherit EOF instead of a live pipe (macOS hang fix
      2026-09-17).
    * macOS hang fix 2026-09-17: the bare subprocess.run() was replaced by a manual Popen
      loop -- stdin is fed from a dedicated thread, stdout/stderr are drained continuously
      (no pipe-fill deadlock), a heartbeat (#8.x) is logged every 10s while the child runs,
      and on timeout the WHOLE process group is killed (POSIX start_new_session + killpg;
      Windows kills the direct child). The raised TimeoutExpired always carries partial
      stdout+stderr so python_exec() can surface the child's last C* marker in the TIMEOUT
      result.

    * Byte-exact payload delivery (2026-09-22): stdin is a BINARY pipe -- text-mode
      pipes on Windows translate newlines on write and corrupted every payload with
      CR/LF bytes. stdout/stderr are drained as bytes and decoded once at the edge.
    """
    env = os.environ.copy()
    for _k in list(env.keys()):
        if _k.startswith("JACK_PYEXEC_"):
            del env[_k]  # never trust inherited sandbox state
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["JACK_PYEXEC_ROOT"] = working_dir

    _tmp_dir = os.path.join(working_dir, ".temp")  # user policy (2026-09-18): every python_exec temp file lands in <working_root>/.temp
    try:
        os.makedirs(_tmp_dir, exist_ok=True)
    except Exception:
        pass  # tempfile falls back to the OS default; bootstrap still enforces root
    # TEMP/TMP are the Windows temp vars; TMPDIR is what macOS/BSD and
    # Python's tempfile module consult on POSIX. Pinning all three keeps
    # tempfile.* and any shell $TMPDIR usage inside the sandboxed tree
    # on every platform (macOS fix 2026-09-15).
    env["TEMP"] = _tmp_dir
    env["TMP"] = _tmp_dir
    env["TMPDIR"] = _tmp_dir

    payload = (
        _CHILD_PREAMBLE
        + SANDBOX_BOOTSTRAP
        + "\n"
        + "_jpsd_log('C2: sandbox bootstrap installed OK')"
    ) + "\n" + wrapped_code

    # PAYLOAD DELIVERY VIA STDIN (2026-09-15): the bootstrap prefix is ~23 KB and
    # Windows CreateProcess caps the whole command line at 32,768 chars -- passing
    # the payload with `python -c <payload>` made any user code above ~9 KB fail
    # with WinError 206 (verified). Feeding the payload on stdin removes the limit
    # entirely and works identically on Windows/macOS/Linux. PYTHONUTF8=1 pins all
    # child stdio to UTF-8 so non-ASCII payloads survive the pipe on every locale.
    env["PYTHONUTF8"] = "1"
    env["JACK_PYEXEC_DEBUG"] = os.environ.get("JACK_PYEXEC_DEBUG", "0")  # ALL [PYEXEC] marker output (parent stderr + log files) OFF by default; opt in with JACK_PYEXEC_DEBUG=1 (2026-09-30 client-console cleanup); forwarded to the child so one switch re-enables parent AND child markers
    env["JACK_PYEXEC_NETWORK"] = os.environ.get("JACK_PYEXEC_NETWORK", "0")  # admin network opt-in (2026-09-30): forwarded to the child sandbox; with =1 its socket stage installs no outbound restrictions. Cleared above with all other JACK_PYEXEC_* state, so only THIS process's env counts.
    # OUT-OF-SANDBOX NETWORK CONSENT GATE (2026-11-24): tell the child whether a UI relay
    # is live for this run. =1 -> its socket gate writes .temp/.jack_netreq/<id>.json and
    # waits for our decision file; =0 -> original instant hard block, no waiting.
    env["JACK_PYEXEC_NET_CONSENT"] = "1" if net_ask_callback is not None else "0"
    _netask_to = os.environ.get("JACK_PYEXEC_NET_ASK_TIMEOUT", "").strip()
    if _netask_to:
        env["JACK_PYEXEC_NET_ASK_TIMEOUT"] = _netask_to  # optional admin override of the child's ask timeout

    _t_dbg = time.time()
    _pyexec_dbg("#7 about to spawn child subprocess (timeout=%ss)" % effective_timeout, working_dir)

    # Child-side debug log is truncated per run so its LAST line always belongs to
    # THIS execution (2026-09-17 macOS hang fix). Only when file logging is opted in --
    # default runs must not create the file at all (2026-09-18 cleanup).
    if env["JACK_PYEXEC_DEBUG"] != "0":
        try:
            _logdir = os.path.join(working_dir, ".temp")  # user policy (2026-09-18): child debug log lives in .temp/, not working_root top level
            os.makedirs(_logdir, exist_ok=True)
            open(os.path.join(_logdir, ".jack_pyexec_child.log"), "w").close()
        except Exception:
            pass

    _is_win = os.name == "nt"
    # POSIX: put the child in its own session/process group so a timeout can kill the
    # WHOLE tree (child + any git/shell grandchildren) instead of only the direct
    # child. Windows has no process groups -- kill the direct child there.
    _popen_kwargs = {}
    if not _is_win:
        _popen_kwargs["start_new_session"] = True

    # The -c command is _CHILD_ENTRY: it reads the payload from stdin and CLOSES fd 0
    # in the child before exec'ing it -- spawned tools then inherit EOF on stdin, not a
    # live pipe (the macOS git-hang fix).
    _cmd = [sys.executable, "-c", _CHILD_ENTRY]
    proc = subprocess.Popen(
        _cmd,
        cwd=working_dir,
        # BINARY pipes (2026-09-22 byte-exact delivery fix): with text=True the
        # child stdin is a Windows CRT TEXT pipe that translates newlines on write
        # (\n -> \r\n, lone \r -> \r\r) -- silently corrupting every payload carrying
        # CR/LF bytes before it reaches the child. Binary mode delivers byte-exact on
        # every platform; encoding/decoding happens explicitly at the edges only.
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if _is_win else 0,
        **_popen_kwargs,
    )

    def _feed_stdin():
        # Dedicated thread: if the interpreter stalls during startup (before it reads
        # stdin), a blocking write here would otherwise run past the deadline and no
        # timeout could ever fire. Chunked so even a 1 MB payload never blocks long.
        try:
            # BINARY delivery (2026-09-22): encode once, write bytes -- no text-mode
            # pipe translation, byte-exact on every platform.
            _payload_b = payload.encode("utf-8")
            for _i in range(0, len(_payload_b), 65536):
                proc.stdin.write(_payload_b[_i:_i + 65536])
            proc.stdin.close()
            # #7.3 (2026-09-17 round 3): payload fully delivered to the child's stdin --
            # if this line never appears, the FEED thread blocked in a write (child not
            # reading) and everything after it is downstream of that stall.
            _pyexec_dbg("#7.3 stdin feed complete (%d chars delivered)" % len(payload), working_dir)
        except Exception as _fe:
            _pyexec_dbg("#7.4 stdin feed error: %r" % (_fe,), working_dir)  # gated like all other markers (2026-09-30)
        finally:
            # Always release the write end, even when the child died early. Without
            # this the pipe (and its handle) leaked until GC.
            try:
                proc.stdin.close()
            except Exception:
                pass

    _out_chunks, _err_chunks = bytearray(), bytearray()

    def _drain(_pipe, _sink):
        # Continuous drain in a dedicated thread per pipe -- prevents the classic
        # "one pipe fills up and blocks the child" deadlock. Lossless: repeated
        # communicate(timeout) polling would discard partial reads on every timeout.
        try:
            while True:
                _chunk = _pipe.read(65536)
                if not _chunk:
                    break
                _sink.extend(_chunk)  # bytes chunk into bytearray sink (binary pipes, 2026-09-22)
        except Exception:
            pass  # pipe closed by kill -- keep whatever was already drained

    _feed_thread = threading.Thread(target=_feed_stdin, daemon=True)
    _out_thread = threading.Thread(target=_drain, args=(proc.stdout, _out_chunks), daemon=True)
    _err_thread = threading.Thread(target=_drain, args=(proc.stderr, _err_chunks), daemon=True)
    _feed_thread.start()
    _out_thread.start()
    _err_thread.start()

    def _close_proc_pipes():
        """Close every pipe exactly once. Prevents ResourceWarning/handle leaks on
        repeated calls (the old code relied on GC for stdout/stderr)."""
        for _p in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if _p is not None:
                    _p.close()
            except Exception:
                pass

    def _child_trace():
        # Child-side debug log (written BY THE CHILD -- C*/B* markers). The parent's own
        # stderr capture is blind while the child runs, so on heartbeat/timeout we read
        # this file and attach its LAST lines to the marker: even mid-run the console
        # shows exactly which bootstrap stage / user line the child reached.
        try:
            _cl = os.path.join(working_dir, ".temp", ".jack_pyexec_child.log")  # user policy (2026-09-18): child debug log lives in .temp/
            if not os.path.exists(_cl):
                return "child log empty (no C1 yet -- stuck before entry or in stdin read)"
            with open(_cl, "r", encoding="utf-8", errors="replace") as _cf:
                _lines = [l for l in _cf.read().splitlines() if l.strip()]
            return (" | ".join(_lines[-3:]) if _lines else "child log empty (no C1 yet)")
        except Exception:
            return "(child trace unavailable)"

    # OUT-OF-SANDBOX NETWORK CONSENT (2026-11-24): remember which request/decision files
    # already existed so we only react to NEW ones written by THIS child during the run.
    _net_req_dir = os.path.join(working_dir, ".temp", ".jack_netreq")
    try:
        _net_pre = set(os.listdir(_net_req_dir))  # files from BEFORE this run (concurrent runs)
    except Exception:
        _net_pre = set()
    _net_seen = set(_net_pre)  # grows as we react to new request/decision files

    def _poll_net_requests():
        # Scan for new request files; each one spawns a daemon thread that asks the UI
        # and writes the decision file. Never blocks the heartbeat/timeout loop below.
        if net_ask_callback is None:
            return
        try:
            for _f in os.listdir(_net_req_dir):
                if not _f.endswith(".json") or _f in _net_seen:
                    continue
                if _f.endswith(".decision.json"):
                    _net_seen.add(_f)  # our own handler's output -- ignore from now on
                    continue
                try:
                    with open(os.path.join(_net_req_dir, _f), "r", encoding="utf-8") as _rf:
                        _req = json.load(_rf)
                except Exception:
                    continue  # half-written file: NOT marked seen -> retried next poll;
                              # a truly foreign file is retried harmlessly until the run ends
                if not isinstance(_req, dict) or "request_id" not in _req:
                    _net_seen.add(_f)  # parsed but not one of ours -- ignore
                    continue
                _net_seen.add(_f)
                threading.Thread(target=net_ask_callback, args=(_req,), daemon=True).start()
        except Exception:
            pass

    _hb_next = _t_dbg + 10.0
    try:
        while proc.poll() is None:
            _poll_net_requests()
            if time.time() - _t_dbg >= effective_timeout:
                raise subprocess.TimeoutExpired(cmd=_cmd, timeout=effective_timeout)
            if time.time() >= _hb_next:
                _pyexec_dbg(
                    "#8.x heartbeat: child pid=%s still running after %.0fs (deadline %ss) | CHILD TRACE: %s"
                    % (proc.pid, time.time() - _t_dbg, effective_timeout, _child_trace()), working_dir)
                _hb_next += 10.0
            time.sleep(0.25)

        # Child exited -- let the drain threads finish reading the pipe tails.
        _out_thread.join(timeout=5)
        _err_thread.join(timeout=5)
        _feed_thread.join(timeout=1.0)
        _close_proc_pipes()
    except subprocess.TimeoutExpired:
        elapsed = time.time() - _t_dbg
        _pyexec_dbg(
            "#9.5 TIMEOUT after %.1fs -- killing child pid=%s (whole process group on POSIX) | CHILD TRACE: %s"
            % (elapsed, proc.pid, _child_trace()), working_dir)
        # Unblock the feed thread first: closing the write end turns a pending
        # pipe write into BrokenPipeError instead of letting it linger.
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            if not _is_win and hasattr(os, "killpg"):
                os.killpg(proc.pid, 9)      # SIGKILL the whole group (child + grandchildren)
            else:
                proc.kill()                 # Windows / no killpg: direct child only
        except Exception as _ke:
            _pyexec_dbg("#9.5.1 kill raised %r" % (_ke,), working_dir)
        try:
            proc.wait(timeout=5)            # reap it; prevents "subprocess still running" leaks
        except Exception:
            pass
        _out_thread.join(timeout=5)
        _err_thread.join(timeout=5)
        _feed_thread.join(timeout=2)
        # Capture BEFORE closing the pipe objects; the chunks live on their own.
        _timeout_out = bytes(_out_chunks).decode("utf-8", "replace")
        _timeout_err = bytes(_err_chunks).decode("utf-8", "replace")
        _close_proc_pipes()
        raise subprocess.TimeoutExpired(
            cmd=_cmd, timeout=effective_timeout,
            output=_timeout_out,
            stderr=_timeout_err)

    # OUT-OF-SANDBOX NETWORK CONSENT cleanup: drop ONLY this run's request/decision
    # files (the delta vs the pre-run snapshot) so .temp/.jack_netreq stays empty
    # between runs. Files that existed BEFORE this run belong to a concurrent run --
    # deleting them would break that run's pending ask (verified audit 2026-11-24).
    if net_ask_callback is not None:
        try:
            for _f in list(_net_seen - _net_pre):
                os.remove(os.path.join(_net_req_dir, _f))
        except Exception:
            pass

    # Pipes were closed right after the drain threads in the normal-exit path.
    # Decode at the edge (2026-09-22): pipes are binary; errors='replace' keeps a
    # stray non-UTF8 byte from ever breaking result assembly.
    out_s = bytes(_out_chunks).decode("utf-8", "replace")
    err_s = bytes(_err_chunks).decode("utf-8", "replace")
    _pyexec_dbg("#8 spawned -> #9 child exited rc=%s after %.1fs" % (proc.returncode, time.time() - _t_dbg), working_dir)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out_s, err_s)


# ============================================================================
# MAIN ENTRY POINT -- python_exec() tool function.
# No relative imports of helper modules at call time; fully sandbox-deliverable.
# ============================================================================

def python_exec(code, timeout=None):
    """Execute Python code in an isolated child process.

    Runs the code as a separate ``python -c`` subprocess pinned to working_root
    (no CWD fallback), captures stdout/stderr, and enforces the active profile's
    security guardrails (import blocklist + AST hard floor + pattern scan; see
    module docstring), and the UNCONDITIONAL runtime sandbox that pins all filesystem
    access to working_root for every execution. An EMPTY profile blocklist skips the
    STATIC guardrails by design, but the runtime sandbox still enforces containment.

    Args:
        code: Python source to execute as a string. May use imports/subprocess/os/
            sys/json/math/re etc., subject to the active guardrails. Output is
            truncated at ~10 KB; errors at ~2 KB.
        timeout: Optional timeout in seconds. Defaults to the configured value
            (FILE_EXEC_TIMEOUT, typically 300). On expiry the process is killed and
            a TIMEOUT message with any partial output is returned.

    Returns:
        str - the subprocess stdout on success ("Executed successfully (no output)"
        when empty). Non-fatal stderr is appended under a clearly marked block.
        Failures return a one-line category, the failing user line + source, a
        cleaned traceback whose line numbers refer to the SUBMITTED code, and any
        stdout produced before the failure. Timeouts include partial stdout/stderr.
    """

    if not isinstance(code, str):
        _pyexec_dbg("#2 params invalid: code is not a string")
        return "Error: code must be a string."

    if not code.strip():
        _pyexec_dbg("#2 params ok but empty -> early success return")
        return "Executed successfully (no output)"

    if len(code) > 1_000_000:
        _pyexec_dbg("#2 params invalid: code exceeds 1 MB (%d chars)" % len(code))
        return "Error: code exceeds 1 MB limit. Please shorten your snippet."

    if "\x00" in code:
        _pyexec_dbg("#2 params invalid: code contains a NUL byte")
        return ("Error: code contains a NUL (\\x00) byte and cannot be compiled. "
                "Remove binary/NUL data from the snippet.")

    try:
        code.encode("utf-8")
    except UnicodeEncodeError as _ue:
        _pyexec_dbg("#2 params invalid: code is not UTF-8 encodable")
        return ("Error: code contains characters that cannot be encoded as UTF-8 "
                "(%s). Replace them with normal Unicode text." % _ue)

    # Validate/coerce timeout BEFORE any spawn: a string, zero, negative or NaN
    # timeout used to fail later inside the poll loop with an opaque TypeError.
    effective_timeout, _timeout_err = _coerce_timeout(timeout)
    if _timeout_err:
        _pyexec_dbg("#2 params invalid: %s" % _timeout_err)
        return _timeout_err

    # DEBUG MARKERS (2026-09-16): numbered checkpoints for hang diagnosis -- see
    # the _pyexec_dbg() docstring above for the full stage map. The LAST number in
    # stderr / <working_root>/.temp/.jack_pyexec_debug.log is where execution stopped.
    import time as _t_pe
    _pe_t0 = _t_pe.time()
    _pyexec_dbg("#1 python_exec() entered (code=%d chars, timeout=%r)" % (len(code), timeout))

    # Resolve working_root FIRST -- execution is pinned to it. Fail-closed: no CWD
    # fallback, ever (2026-07-15).
    try:
        _pyexec_dbg("#2 params ok -> resolving working_root")
        working_dir = _get_working_dir()
        _pyexec_dbg("#3 working_root resolved: %s" % working_dir, working_dir)
    except Exception as e:
        return (
            f"Error: cannot determine a valid working_root for python_exec ({e}). "
            f"The tool must stay inside working_root and will NOT fall back to the "
            f"process CWD. Configure working_root first."
        )

    # Per-profile blocked-libs sets (2026-08-20 feature). The profile decides which
    # modules are import-blocked; the set file is loaded by the SERVER and shipped
    # with tools_response ("python_exec_blocked_libs"). Semantics:
    #   - non-empty list -> only those modules are import-blocked (function/attr/
    #     pattern guardrails below stay active as defense-in-depth, INCLUDING THE
    #     HARD FLOOR -- see _ast_guard.py docstring), plus the path-scope pre-exec
    #     scan (see _path_scope_enabled())
    #   - EMPTY list     -> admin clean set: NO restrictions at all. Every guardrail
    #     below is skipped BY DESIGN -- only what a profile explicitly lists may be
    #     blocked (user requirement, 2026-08-26 fix). The 2026-08-23 path-scope check
    #     used to run even for the empty set and kept blocking subprocess work that
    #     passed absolute paths -- that was the bug this restructure removes.
    #   - None/unavailable -> built-in DEFAULT_FORBIDDEN_MODULES (fail-safe defaults)
    active_blocked_libs = _get_blocked_libs()  # resolve ONCE for the whole run
    _pyexec_dbg("#4 blocked-libs set resolved (%d modules active)" % len(active_blocked_libs), working_dir)

    if active_blocked_libs:  # non-empty blocklist = profile restricted -> guardrails on

        # PATH-SCOPE GUARDRAIL (2026-08-23, narrowed 2026-10-23):
        # pre-exec scan of path arguments that resolves each literal against
        # working_root. Only literals actually passed to a filesystem API (open,
        # os.path.*, shutil.*, Path(...), ...) or to a path-named keyword are
        # checked -- see run_path_scope_check() in _ast_guard.py for the contract.
        # This removes the old false positives where ANY string starting with an
        # absolute-looking path (print("/api/v1/users"), a Windows path in a log
        # message, ...) blocked perfectly safe code. The runtime sandbox below is
        # still the hard boundary for dynamically built paths.
        if _path_scope_enabled():
            _pyexec_dbg("#5 path-scope pre-exec check running", working_dir)
            try:
                scope_bad, scope_violations = run_path_scope_check(code, working_dir)
            except Exception as e:  # the guardrail itself must never fail open
                return (f"EXECUTION BLOCKED by path-scope guardrail error ({e}). "
                        f"The pre-execution path check could not complete; failing closed.")
            if scope_bad:
                violation_list = '\n'.join(f'  * {v}' for v in scope_violations)
                return (
                    f"EXECUTION BLOCKED by path-scope guardrail.\n\n"
                    f"The code references paths OUTSIDE the working_root "
                    f"({working_dir}) and was NOT executed:\n\n"
                    f"{violation_list}\n\n"
                    f"All filesystem access in python_exec must stay inside working_root. "
                    f"Use relative paths (resolved against working_root) or subfolders of it."
                )

        _pyexec_dbg("#5.5 AST + pattern security checks running", working_dir)
        is_forbidden, violations = _run_security_checks(code)
        _pyexec_dbg("#6 security checks passed (no violations)", working_dir)
        if is_forbidden:
            violation_list = '\n'.join(f'  * {v}' for v in violations)
            return (
                f"EXECUTION BLOCKED by security guardrail.\n\n"
                f"The following forbidden patterns were detected:\n\n"
                f"{violation_list}\n\n"
                f"This code cannot be executed. The python_exec tool is restricted "
                f"to calculations, data processing, and safe operations only. "
                f"File I/O, subprocess calls, network access, and other dangerous "
                f"operations are not allowed by this profile.\n\n"
                f"If you need to edit a file, use the read_file / write_file tools "
                f"instead of open() inside python_exec; if this is unexpected, ask "
                f"the user or an admin to review the profile's python_exec blocked-libs "
                f"set."
            )

    # Execute in isolated subprocess with the UNCONDITIONAL runtime sandbox:
    # the bootstrap prefix pins all filesystem access to working_root at runtime,
    # for every profile including the empty admin clean set (2026-08-31 hard floor).
    # effective_timeout was validated/coerced at the top of this function.
    os.makedirs(working_dir, exist_ok=True)

    # OUT-OF-SANDBOX NETWORK CONSENT (2026-11-24): when this call came through the CLIENT
    # orchestrator with a live chat context, build the UI relay callback so the sandbox's
    # socket gate can ASK THE USER instead of hard-blocking. No context -> None -> the
    # child keeps its original instant block (JACK_PYEXEC_NET_CONSENT=0).
    _net_ctx = _get_caller_context()
    net_ask_callback = _make_net_ask_callback(_net_ctx, working_dir) if _net_ctx else None

    # Build the child-side block: the user's source is compiled as-is (filename
    # "<python_exec>") so line numbers and decorators/future imports are preserved.
    wrapped_code = _wrap_user_code(code)

    try:
        proc = _run_sandboxed_subprocess(wrapped_code, working_dir, effective_timeout,
                                         net_ask_callback=net_ask_callback)

        # Do NOT strip leading whitespace: it is data for tables/JSON/CSV. The
        # formatters only remove trailing newlines and normalize CRLF.
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        # (2026-07-18 unified consent dialog) surface any "Explain request" instructions the
        # user triggered on file/network cards DURING this run, so the AI gets them in THIS
        # tool result and can answer + re-issue python_exec.
        _explain_block = drain_explain_instructions()
        _pyexec_dbg("#10 output captured (stdout=%d, stderr=%d chars), total %.1fs" % (len(stdout), len(stderr), time.time() - _pe_t0), working_dir)

        if proc.returncode != 0:
            _pyexec_dbg("#11 returning non-zero rc=%s result" % proc.returncode, working_dir)
            return _format_failure(code, proc.returncode, stdout, stderr) + _explain_block

        _pyexec_dbg("#11 returning success result (%d chars), total %.1fs" % (len(stdout) if stdout else 0, time.time() - _pe_t0), working_dir)
        return _format_success(stdout, stderr) + _explain_block

    except subprocess.TimeoutExpired as exc:
        _pyexec_dbg("#9.5 TIMEOUT fired after %ss -- child did not finish in time" % effective_timeout, working_dir)
        # Always append any Explain instruction stashed by a consent card during the run.
        return _format_timeout(exc, effective_timeout) + drain_explain_instructions()

    except Exception as e:
        _pyexec_dbg("#12 EXCEPTION in python_exec(): %r (total %.1fs)" % (e, time.time() - _pe_t0), working_dir)
        import traceback
        tb = _to_lf(traceback.format_exc(), lone_cr=True)
        return _truncate(
            "Error: python_exec itself failed before returning a result: %s\n\n%s"
            % (str(e), tb),
            FILE_EXEC_ERROR_TRUNCATE_CHARS,
            "traceback truncated",
        ) + drain_explain_instructions()
