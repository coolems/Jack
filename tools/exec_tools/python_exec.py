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
        "description": "Execute Python code. Use for calculations, data processing, git commands, or any task that requires running Python code.",
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

import logging
import os
import subprocess
import sys
import textwrap
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
#                          installed OK -> C4a/C4b per-user-line tracer (flat code only:
#                          "line N of M" = which line of YOUR code was executing when it
#                          stopped) -> C5 user code finished.
#   Child markers ALSO append to <working_root>/.temp/.jack_pyexec_child.log from the child
#   itself, so they survive even when the parent captures stderr into a pipe and never
#   returns it -- that is what made Mac hangs at #7 blind. After a hang, read that file:
#   its LAST line says exactly where the child stopped (C1 = bootstrap stage, C4a N/M =
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
# C4a/C4b N/M = which top-level statement of YOUR code was executing/finished).

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



def _wrap_user_code(user_code):
    """Wrap user code in __main__ guard for subprocess execution.

    The ``if __name__ == "__main__":`` wrapper is what makes globals()['__builtins__']
    a MODULE inside the child (script context) -- which is exactly why the hard floor
    in _ast_guard.py treats that expression as dangerous. Do not remove it without
    re-auditing the guardrail battery (tests/test_python_exec_round3.py and
    tests/test_sandbox_bootstrap.py).

    Every top-level statement additionally gets C4a/C4b breadcrumbs via _jpsd_log so a
    hang localizes to the exact user statement (2026-09-17 macOS hang fix; round 4 made
    this STRUCTURE-AWARE): markers are injected only BETWEEN complete top-level
    statements, using AST line ranges on the RAW source -- never inside an indented
    block and never splitting a multi-line statement. The old per-LINE injection broke
    any snippet containing indentation with an IndentationError (verified 2026-09-17),
    which is why structured code previously had zero breadcrumbs: on Mac it hung at
    'C2: sandbox bootstrap installed OK' and the stopping line could not be localized.
    Unparseable code gets no markers -- it fails naturally with its own SyntaxError.
    """
    lines = user_code.strip().splitlines() or ["pass"]

    # AST-based statement ranges (1-based, inclusive). A marker may only sit where a
    # top-level statement has fully ended / is about to start -- that keeps multi-line
    # statements (with/for/if bodies, backslash continuations) intact. NOTE: parse the
    # STRIPPED text joined back together -- ast line numbers must match `lines` exactly;
    # parsing user_code directly would offset every number by its leading blank lines
    # (verified bug 2026-09-17 round 4: marker landed inside a def body).
    import ast as _ast_dbg
    try:
        _tree_dbg = _ast_dbg.parse("\n".join(lines))
    except SyntaxError:
        _tree_dbg = None

    body_lines = []
    if _tree_dbg is not None and getattr(_tree_dbg, "body", []):
        ranges = []
        for _st in _tree_dbg.body:
            try:
                ranges.append((_st.lineno, _st.end_lineno))
            except Exception:
                pass
        start_no = dict((s, n) for n, (s, e) in enumerate(ranges, 1))
        end_no = dict((e, n) for n, (s, e) in enumerate(ranges, 1))
        total = len(ranges)
        for _li, _ln in enumerate(lines, 1):
            if _li in start_no and lines[_li - 1].strip() != "":
                body_lines.append(
                    "_jpsd_log('C4a: child executing user statement %d/%d')" % (start_no[_li], total)
                )
            body_lines.append(_ln)
            if _li in end_no and lines[_li - 1].strip() != "":
                body_lines.append(
                    "_jpsd_log('C4b: child finished user statement %d')" % end_no[_li]
                )
    else:
        # No AST (unparseable) -- emit the raw lines only; the run will fail naturally.
        body_lines = list(lines)

    indented = textwrap.indent("\n".join(body_lines), "    ")
    return (
        'if __name__ == "__main__":\n'
        + indented
        + "\n    _jpsd_log('C5: child user code finished')"
    )


def _run_sandboxed_subprocess(wrapped_code, working_dir, effective_timeout):
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

    _hb_next = _t_dbg + 10.0
    try:
        while proc.poll() is None:
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
    except subprocess.TimeoutExpired:
        elapsed = time.time() - _t_dbg
        _pyexec_dbg(
            "#9.5 TIMEOUT after %.1fs -- killing child pid=%s (whole process group on POSIX) | CHILD TRACE: %s"
            % (elapsed, proc.pid, _child_trace()), working_dir)
        try:
            if not _is_win and hasattr(os, "killpg"):
                os.killpg(proc.pid, 9)      # SIGKILL the whole group (child + grandchildren)
            else:
                proc.kill()                 # Windows / no killpg: direct child only
        except Exception as _ke:
            _pyexec_dbg("#9.5.1 kill raised %r" % (_ke,), working_dir)
        _out_thread.join(timeout=5)
        _err_thread.join(timeout=5)
        _feed_thread.join(timeout=2)
        raise subprocess.TimeoutExpired(
            cmd=_cmd, timeout=effective_timeout,
            output=bytes(_out_chunks).decode("utf-8", "replace"),
            stderr=bytes(_err_chunks).decode("utf-8", "replace"))

    _feed_thread.join(timeout=1.0)
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
        when empty), or an "Error:" / "EXECUTION BLOCKED" / "EXECUTION TIMEOUT"
        message describing what went wrong.
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

        # PATH-SCOPE GUARDRAIL (2026-08-23, gated by the per-profile set since 2026-08-26):
        # pre-exec scan of all string literals; any path that resolves OUTSIDE
        # working_root (or a '..' traversal) blocks the run before it starts. Skipped
        # when the profile set is EMPTY (admin clean set).
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
                f"operations are not allowed."
            )

    # Execute in isolated subprocess with the UNCONDITIONAL runtime sandbox:
    # the bootstrap prefix pins all filesystem access to working_root at runtime,
    # for every profile including the empty admin clean set (2026-08-31 hard floor).
    effective_timeout = timeout if timeout is not None else FILE_EXEC_TIMEOUT
    os.makedirs(working_dir, exist_ok=True)

    wrapped_code = _wrap_user_code(code)  # injects child-side C4a/C4b/C5 markers

    try:
        proc = _run_sandboxed_subprocess(wrapped_code, working_dir, effective_timeout)

        stdout = _safe_strip(proc.stdout)
        stderr = _safe_strip(proc.stderr)
        _pyexec_dbg("#10 output captured (stdout=%d, stderr=%d chars), total %.1fs" % (len(stdout), len(stderr), time.time() - _pe_t0), working_dir)

        if proc.returncode == 3:
            # Fail-closed init failure in the bootstrap (missing/invalid root).
            _pyexec_dbg("#11 returning fail-closed rc=3 result", working_dir)
            detail = stderr or stdout
            return (
                "EXECUTION BLOCKED by python_exec runtime sandbox (fail-closed init).\n\n"
                f"{detail[:FILE_EXEC_ERROR_TRUNCATE_CHARS]}\n\n"
                "The child process could not verify the working_root lock and was "
                "terminated before any code ran. Configure working_root first."
            )

        if proc.returncode != 0:
            _pyexec_dbg("#11 returning non-zero rc=%s result" % proc.returncode, working_dir)
            error_msg = stderr if stderr else stdout
            if not error_msg:
                error_msg = f"Process exited with code {proc.returncode} (no output)"
            truncated = error_msg[:FILE_EXEC_ERROR_TRUNCATE_CHARS]
            return f"Error:\n\n{truncated}"

        _pyexec_dbg("#11 returning success result (%d chars), total %.1fs" % (len(stdout) if stdout else 0, time.time() - _pe_t0), working_dir)
        if not stdout:
            return "Executed successfully (no output)"

        if len(stdout) > FILE_EXEC_OUTPUT_TRUNCATE_CHARS:
            return stdout[:FILE_EXEC_OUTPUT_TRUNCATE_CHARS] + "\n\n...[truncated, output too long]..."

        return stdout

    except subprocess.TimeoutExpired as exc:
        _pyexec_dbg("#9.5 TIMEOUT fired after %ss -- child did not finish in time" % effective_timeout, working_dir)
        timeout_msg = (
            f"EXECUTION TIMEOUT: Code execution exceeded {effective_timeout} seconds.\n\n"
            f"Your Python code took too long to complete and was terminated.\n\n"
            f"IMPORTANT: You need to optimize your code. Consider:\n"
            f"  - Reduce the amount of data processed\n"
            f"  - Break the task into smaller steps\n"
            f"  - Remove unnecessary loops or heavy operations\n"
            f"  - Use more efficient algorithms and data structures\n"
            f"  - Add early exit conditions\n\n"
            f"Please rewrite your code with a shorter execution time and try again."
        )

        partial = ""
        if exc.stdout:
            raw = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode(errors="replace")
            partial = _safe_strip(raw)
        if partial:
            timeout_msg += f"\n\n--- Partial output (before timeout) ---\n{partial[:FILE_EXEC_OUTPUT_TRUNCATE_CHARS]}"

        # Child debug trace (2026-09-17 macOS hang fix): on POSIX the old TimeoutExpired
        # carried NO partial output, so exactly when a run timed out we lost the child's
        # last C* marker -- the one line that says where it hung. The manual Popen loop
        # now raises with stderr attached; surface it (it holds the C1..C5 trail).
        if exc.stderr:
            raw_err = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
            err_partial = _safe_strip(raw_err)
            if err_partial:
                timeout_msg += f"\n\n--- Child debug trace (stderr before kill; last marker = where it stopped) ---\n{err_partial[-FILE_EXEC_ERROR_TRUNCATE_CHARS:]}"

        return timeout_msg

    except Exception as e:
        _pyexec_dbg("#12 EXCEPTION in python_exec(): %r (total %.1fs)" % (e, time.time() - _pe_t0), working_dir)
        import traceback
        tb = traceback.format_exc()
        return f"Error: {str(e)}\n\n{tb[:FILE_EXEC_ERROR_TRUNCATE_CHARS]}"
