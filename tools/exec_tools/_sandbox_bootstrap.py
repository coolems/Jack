r"""python_exec RUNTIME SANDBOX -- data-only module exposing the bootstrap prefix source.

WHAT THIS IS
============
A self-contained Python source string (``SANDBOX_BOOTSTRAP``) that python_exec.py
PREPENDS to every executed payload, so it runs in the child process BEFORE any user
code and installs runtime containment for the lifetime of the run.

CROSS-PLATFORM CONTRACT (Windows + macOS, 2026-09-15 revision; hardened 2026-09-17)
===================================================================================
The bootstrap uses ONLY portable CPython stdlib -- ``sys.addaudithook`` plus
builtin/module patching -- with NO platform-specific APIs. It therefore runs
identically on Windows and macOS (Intel or Apple Silicon). The 2026-08-31
revision that broke on Mac is fixed in two places:

* python_exec.py now pins ``TMPDIR`` as well as TEMP/TMP to
  <working_root>/.temp (2026-09-18: renamed from .jack_pyexec_tmp) -- on POSIX,
  Python's tempfile module and any shell $TMPDIR usage consult TMPDIR, so temp
  files land inside the sandboxed tree there too.
* READ_ROOTS below now include EVERY ``sys.path`` entry (in addition to the
  Python-home tree). A macOS Homebrew python reads its stdlib from /opt/homebrew
  or /usr/local, which is NOT under dirname(sys.executable)/sys.prefix/
  sys.base_prefix -- without this fix ordinary imports died at runtime on Mac.

WHY A RUNTIME LAYER (and not just static scans)
===============================================
The existing guardrails (_ast_guard / _pattern_scan / path-scope) are STATIC: they
inspect source text before execution and are, by their own documented admission,
whack-a-mole -- any single missed pattern is a full escape because the code then runs
in an ordinary interpreter. A verified live exploit read a system file OUTSIDE working_root (rc=0).

This module closes that gap at RUNTIME: every file open / enumeration / mutation and
every native-code import is checked against working_root AT THE MOMENT IT HAPPENS,
using the SAME containment semantics as tools/path_guard.py (realpath + separator-
anchored prefix). It is an unconditional HARD FLOOR -- it runs for every execution,
including the EMPTY admin clean set that skips all static guardrails by design.

CONTAINMENT SEMANTICS  *** SYNC NOTE: keep identical to tools/path_guard.py::_is_path_inside_allowed ***
------------------------------------------------------------------
* A path is "inside" a root iff ``normcase(realpath(path))`` equals the root or starts
  with ``root + os.sep`` (separator-anchored -- prevents prefix attacks like
  working_root+"Evil"; the root is rstrip'd first so a drive root 'C:\' does not
  become 'C:\\' + sep). realpath resolves symlinks, UNC and .. sequences, and
  normcase folds Windows case on BOTH sides -- so a case-different spelling of
  working_root and a symlink/junction working_root contain their own files.
* WRITE operations (open w/a/x/+, os.open write flags, nt.open write flags,
  remove/rename/mkdir/..., shutil copy/move/copytree/rmtree destinations, archive
  extraction targets) are allowed ONLY inside working_root. No exceptions.
* READ / enumeration operations (open r, listdir/scandir/walk) are allowed inside
  working_root PLUS the Python-home tree (dirname(sys.executable), sys.prefix,
  sys.base_prefix) PLUS every sys.path entry. The read allowance is REQUIRED so that
  stdlib imports keep working in the child on any platform; it only exposes Python's
  own installation and its site-packages, never user data.
* METADATA calls (os.stat/lstat/os.access/readlink and their os.path / pathlib / nt
  wrappers) are SCOPED (2026-09-17 hardening): stat-family reads allow READ_ROOTS;
  existence/size predicates (exists/isfile/isdir/getsize, Path.exists/...) answer
  only for paths inside working_root -- outside-root probes raise FileNotFoundError /
  return False instead of leaking path EXISTENCE or file SIZE. Windows subprocess
  PATH resolution is unaffected: it uses CreateFile on the executable via _winapi at
  C level, never a Python-level stat call (verified 2026-09-17). CONSENT FIX (2026-10-12):
  the metadata wrappers now honor an explicit ALLOW decision from the OUT-OF-SANDBOX
  FILE CONSENT GATE -- once the local user approved an outside path/dir in a dialog,
  stat/exists/isdir/getsize report reality for it (no more lying after approval).
  Unapproved outside paths still fail closed exactly as before; metadata probes never
  trigger new consent dialogs themselves (they must stay cheap and side-effect free).

HARDENING 2026-09-17 -- CLOSING THE VERIFIED shutil / RAW-WIN32 / METADATA ESCAPES
==================================================================================
Live exploits that this revision closes (Windows, admin clean set):
* ``shutil.copy2(src, '<OTHER_DRIVE>:/x')`` SUCCEEDED with rc=0: on Windows 3.8+ copy2 uses the
  native ``_winapi.CopyFile2`` fast path -- a C call that fires NO Python "open" audit
  event and bypasses every open-level check. (copy() fell back to plain open() and was
  already blocked; move() cross-drive falls back to copy2 -> same hole.)
* Direct ``_winapi.CopyFile2(src, '<OTHER_DRIVE>:/x')`` SUCCEEDED -- the raw function is exposed
  because _winapi is PRELOADED into sys.modules before setup runs (verified), so an
  import-block alone cannot reach it.
* ``nt.open(path, O_WRONLY|O_CREAT)`` write-flags were misread as READ mode by the
  "open" audit event (it assumed a string mode) -- fixed with flag-aware detection.
* ``os.path.exists('<OTHER_DRIVE>:/')`` returned True and ``os.stat(r'<SYSTEM_DIR>\\sensitive.txt').st_size``
  leaked real file metadata from outside working_root; nt.stat leaked the same way.

Fixes (all in the child, all fail-closed):
1. AUDIT "open" event is now FLAG-AWARE: string modes (builtins.open) vs int flag
   words (os.open / nt.open / io.FileIO) -- write flags => ROOT only.
2. HIGH-LEVEL FILE FUNCTIONS are wrapped with source/destination validation BEFORE
   their C fast paths run: shutil.copyfile/copy/copy2/move (src read-side, dst
   write-side), copytree, rmtree, make_archive/unpack_archive, disk_usage. The same
   checks are registered as AUDIT events for shutil.copyfile / shutil.move -- a second
   net that fires even if user code holds a reference to the ORIGINAL function.
3. RAW WIN32 PRIMITIVES are patched at the function level (the import block is a
   no-op because nt/_winapi are preloaded): nt.open (flag-aware), nt.stat/lstat,
   nt.access, _winapi.CopyFile2, _winapi.CreateFile (access-mask aware; undetermined
   access fails CLOSED as write).
4. ARCHIVE EXTRACTORS validate archive path + every member path: zipfile.ZipFile /
   extract / extractall (absolute members and '..' traversal denied; URL schemes like
   'https://...' denied in the constructor), tarfile.TarFile / extract / extractall.
5. COMPRESSED-FILE WRITERS are scoped by mode: gzip/bz2/lzma.open -- write modes
   ROOT-only, read modes READ_ROOTS.
6. METADATA IS SCOPED (see CONTAINMENT SEMANTICS): os.stat/os.lstat/os.access/
   os.readlink plus the os.path and pathlib wrappers.
7. IMPORT BLOCKS in the child now also list nt/_winapi/winreg -- a defense-in-depth
   net for builds where they are NOT preloaded (they ARE preloaded on CPython 3.12
   Windows, which is why fix #3 above patches the functions directly).

HARDENING 2026-11-14 -- CONSENT SELF-APPROVAL, RAW-WIN32/CLOSURE, FAIL-OPEN STAGES
==================================================================================
Live exploits reproduced inside a real sandboxed child (admin clean set, NET_CONSENT=0),
all closed here:

* CONSENT GATE SELF-APPROVAL. The three out-of-sandbox gates (network / file / subprocess)
  read the parent's answer from <root>/.temp/.jack_netreq/<id>.decision.json -- a folder
  INSIDE working_root, which the child can write, and nothing checked who wrote it. A thread
  inside the payload wrote its own {"decision":"allow"}: socket.connect(192.0.2.1:80) passed
  and open(<outside>,"w") returned ALLOWED. DECISIONS ARE NOW PARENT-OWNED: every write route
  (audit "open" flag-aware, os.open, nt.open, _winapi.CreateFile, builtins.open, Path write
  methods, shutil.move, and mutation ops that could REPLACE a file) hard-denies a path whose
  directory component is '.jack_netreq' and whose name ends in '.decision.json'. Reads stay
  legal (the gates read them); the child may still write its own REQUEST file.
* AUDIT "open" WRITE MISREAD. The hook read only args[1]; os.open/nt.open fire
  (path, None, FLAGS_INT), so every raw write looked like a READ at hook level. Now the flag
  word (args[2]) is consulted too.
* RAW WIN32 ENUMERATION / PREDICATES. Stage 6 patched only nt.open/stat/lstat/access:
  nt.listdir(<Windows System32>) -> 4892 entries, nt.scandir -> 4892,
  nt._path_exists/_path_isdir/_path_isfile -> True, nt._getfinalpathname leaked the resolved
  path. Those fire NO audit event at all. Now patched with the same containment semantics
  (listdir/scandir -> READ_ROOTS; predicates answer ROOT-only; pathname info -> READ_ROOTS).
* BYTES PATHS IN THE ENUMERATION HOOK (2026-11-14 follow-up, verified escape). The
  "os.listdir/os.scandir/os.walk" hook branch did ``s = str(args[0])``. On a BYTES path that
  yields ``"b'C:\\Windows'"`` -- a string that is not a path at all -- and ``realpath()``
  resolves it as a NON-EXISTENT CHILD OF THE ROOT, so ``_inside()`` answered True and the
  check passed. Verified live: ``os.listdir(b"C:\\Windows")`` -> 115 entries,
  ``os.scandir(b"C:\\Windows")`` -> 115, ``next(os.walk(b"C:\\Windows"))`` -> 3 tuples / 83
  subdirectories, rc=0 and NO consent request written. Bytes is a legal path type for these
  APIs (str/bytes/PathLike), so the branch now decodes with ``_path_str()`` -- exactly like
  the "open" branch and the nt.* wrappers. Second issue in the same branch: a call with NO
  path argument fires the event with ``args[0] = None`` (verified), and ``os.listdir`` /
  ``os.scandir`` are bound to the ORIGINAL ``nt.listdir``/``nt.scandir`` objects by
  ``from nt import *`` at import time, so the stage-6 ``nt.*`` patch cannot reach those calls
  -- the hook is the only layer on that route. ``str(None)`` is ``"None"``, which resolves as
  ``<root>\None`` -- NOT inside the read roots -- so every zero-argument enumeration was
  DENIED, including the legitimate ``os.listdir()`` inside working_root (false positive,
  verified). It never leaked, but containment on that route rested on a coincidence instead
  of the real path. The branch now resolves the inherited cwd when no path is given (same
  rule as ``_check_cwd()``): in-root default enumeration works again, and a cwd moved outside
  working_root by ``os.chdir()`` is still denied.
* WRAPPER CLOSURE EXTRACTION. Every function wrapper keeps the pre-patch original in its
  __closure__ cells, so `fn.__closure__` recovered raw open / nt.open / _winapi.CreateFile /
  socket.connect and bypassed the wrapper entirely (verified: outside read, outside handle,
  non-loopback connect). Fix philosophy: enforce at the AUDIT level, which fires INSIDE the C
  function and therefore holds however the call was reached -- new hook branches for
  _winapi.CreateFile (access mask AND creation disposition via _winapi_is_write),
  _winapi.CreateProcess (executable containment + program denylist), socket.connect /
  connect_ex / sendto (loopback rule + consent gate, honouring the admin _NET_OPEN opt-in),
  os.system and os.spawn. The wrappers remain as a second net.
* _winapi.CreateProcess was completely unpoliced (a raw spawn bypassed subprocess, so the
  Popen event never fired). Its audit args expose only (executable, marker, start_info) -- NOT
  the command line -- so the hook polices the executable, and a call with executable=None (the
  subprocess shape, program inside the command line) is denied unless this run's subprocess
  Popen event just authorised a spawn: a single-use token (_SPAWN_TOKEN) is granted by the
  Popen branch and consumed by CreateProcess. The stage-6 wrapper sees the real command_line
  and polices the program basename + every absolute-path argument.
* EXEC POLICY ARGUMENTS. The policy inspected only argv[0]'s basename, so arguments were
  unchecked: `findstr localhost <Windows hosts file>` printed the system
  HOSTS file (rc=0). Now _check_cmd_paths() requires every ABSOLUTE path argument of a spawned
  program to resolve inside working_root or a legitimate read root / PATH dir (consentable
  otherwise); relative args resolve against the child cwd, so they stay inside by construction.
* SPAWN CWD (2026-11-14 follow-up, verified escape). _check_cmd_paths() cannot see `cwd=`:
  with cwd outside working_root the argv entries are RELATIVE, so no absolute path appears
  anywhere. `subprocess.run(["findstr","16-bit","win.ini"], cwd=r"C:\Windows")` returned rc=0
  and printed '; for 16-bit app support' -- real content of C:\Windows\win.ini -- and the
  string form `subprocess.run("findstr 16-bit win.ini", cwd=r"C:\Windows")` leaked the same.
  The grandchild is an unsandboxed OS process whose working directory is the leak. Now
  _check_cwd() requires the spawn directory to resolve inside working_root, enforced on every
  route: the "subprocess.Popen" audit event (cwd is args[2] on Windows AND POSIX -- verified
  against Lib/subprocess.py:1534/:1823), the patched Popen.__init__ (keyword or positional
  slot), the "_winapi.CreateProcess" audit event (args[2] -- verified shape
  (None, '\x05', 'C:\\Windows')) and its stage-6 wrapper, plus the os.system / os.spawn audit
  branches, which fire NO Popen event and therefore inherit OUR cwd. A spawn with no explicit
  cwd is checked against os.getcwd() too, so `os.chdir(<outside>)` followed by a bare spawn is
  blocked as well. Askable through the FILE consent gate; in-root cwd (including .temp and
  in-root chdir) keeps working (verified).
* RENAMED SHELLS. cmd.exe copied to 'mycmd.exe' ran '/c echo hi' (rc=0) -- the exact-name
  denylist never saw 'cmd'. Shell stems are now matched as SUFFIXES (_SHELL_SUFFIXES), so a
  renamed shell binary is still denied.
* DEVICE NAMESPACE. _is_dev_or_pipe exempted the whole ``\\.\`` namespace, which covers raw volume
  handles (``\\.\PHYSICALDRIVE0``, ``\\.\HarddiskVolume1``) -- real OS objects outside containment and
  outside the loopback socket rule. Now only ``\\.\pipe\*`` (asyncio stdio transports need it) plus
  the classic console device names are exempt.
* tarfile extractall validated NOTHING when members is None (`for m in members or []`;
  zipfile uses self.namelist()), so a tar carrying '../x' extracted straight out of
  working_root. It now validates self.getmembers() in that default case, and
  filter="fully_trusted" is denied outright.
* EVERY CONTAINMENT STAGE WAS FAIL-OPEN. 11 stage/mod-level handlers swallowed their
  exception and continued, and the hook swallowed non-PermissionError. With addaudithook
  raising, the child ran FULLY UNSANDBOXED with rc=0. Now: a hook that cannot install, or any
  stage/mod patch that raises, calls _jps_fail() -> log + stderr marker + os._exit(3) before
  any user code runs; and an internal error while handling a POLICED event (_POLICED_EVENTS)
  denies that operation instead of letting it through. Unpoliced CPython events still pass, so
  normal runs are unaffected. Logging-only handlers stay tolerant (a log must never break the
  sandbox).

HARDENING 2026-11-15 -- EXEC POLICY: SYSTEM-ABUSE TOOLS, SWITCH ARGUMENTS, "sh" SUFFIX
=======================================================================================
Three verified defects in the exec policy (reproduced with the bootstrap's own
_is_denied_prog()/_check_cmd_paths() and with real spawns of renamed copies in .temp):

* WINDOWS SYSTEM-ABUSE UTILITIES WERE ALLOWED. _DENY_PROGS covered shells, interpreters
  and network transfer tools only -- none of the Windows abuse surface. Verified allowed
  before the fix: wsl, reg, certutil, mshta, net, sc, rundll32, tasklist. `wsl` alone
  defeats the whole sandbox: the Linux side of the machine has no audit hook, so one
  argument hands the payload an unsandboxed filesystem + network + interpreter. `reg`
  writes the registry, which is not a filesystem path and therefore no path check can
  ever cover it. Now denied: wsl, reg, bitsadmin, certutil, mshta, net/net1, netsh,
  schtasks, taskkill, tasklist, sc, msiexec, rundll32.
  (bitsadmin/schtasks/taskkill/msiexec/netsh had been blocked only BY ACCIDENT -- see the
  next bullet -- so the denylist is what makes them reliably denied.)
* WINDOWS SWITCHES MISREAD AS PATH ARGUMENTS. _check_cmd_paths() tested
  ntpath.isabs(tok), and isabs("/list") is True (drive-relative), so every Windows switch
  looked like an absolute path and was denied. Verified false positives on the pre-fix
  bootstrap: `sort /r`, `findstr /c:x README.md`, `findstr /m def README.md`. A switch now
  skips: leading separator + an ALPHA-ONLY flag name, optionally ':value' (values may hold
  dots/digits -- `/c:foo.txt`, `/d:2024-01-01`). A genuine path argument keeps being checked:
  a further separator ('/Windows/win.ini'), a dot in the flag itself ('/win.ini' -> the real
  file C:\win.ini), or a drive/UNC prefix -- so `findstr hello C:\Windows\win.ini` is still
  denied. POSIX is untouched: "/tmp" really is absolute there.
  RESIDUAL: a switch VALUE that looks like a path ('/c:\Windows\win.ini', '/d:C:\x') is not
  inspected -- the flag name is alpha, so the token is treated as a switch. Windows tools
  read such values as literal strings ('/c:' is a search string, not a file), and the tools
  that DO act on a path value (msiexec, certutil, reg) are denied outright by _DENY_PROGS.
* "sh"/"ash" AS SUFFIXES DENIED LEGITIMATE PROGRAMS. The renamed-shell rule matched shell
  stems as suffixes, and 'sh' is a substring of ordinary names -- verified DENIED: finish,
  flash, push, wash, brush, gosh, relish, notesh. Real shells (sh/bash/zsh/dash/ksh/csh/
  tcsh/ash) plus ssh/fish/slogin are now EXACT basenames in _DENY_PROGS; the suffix list
  keeps only stems that cannot occur inside a legitimate name.
  RESIDUAL: a renamed shell whose basename is not an exact match ('mysh.exe') is not
  caught by the suffix list anymore. Accepted deliberately -- restoring 'sh' as a suffix
  would deny legitimate tools again. cmd/powershell/pwsh/wscript/cscript/conhost/bash/zsh/
  dash/ksh/csh/tcsh renamed copies stay denied by suffix.

PERF 2026-09-17 ROUND 4 -- CLOSING THE macOS B1->B2 HANG (realpath recursion storm)
===================================================================================
On POSIX os.path.realpath is PURE PYTHON and lstats every path component; each of
those lstats went through the patched stat/lstat wrapper, which re-validated the
prefix with ANOTHER full realpath -> ~O(N^3) syscalls per containment check (N =
path depth). On a deep Homebrew / Apple-Silicon stdlib tree that made every import
in stage 2 take minutes: child runs stalled between B1 and B2 forever (Windows never
showed it -- ntpath.realpath is one C syscall, _getfinalpathname). Fixed with a
thread-local re-entrancy flag (the patched wrappers call the ORIGINAL function while
our own _norm() validation is running; the outer check validates the final resolved
result -- containment semantics unchanged) plus an LRU cache on _norm() and
precomputed PATH-dir/PATHEXT data for _in_path_dirs. Stage 2 now also logs B2a..B2e
sub-markers so any future stall pins to one patch block.

CHILD-PROCESS EXEC POLICY (2026-09-15, closes the verified macOS escape)
========================================================================
Verified exploit (Mac, admin clean set): direct open() was blocked by this sandbox,
but ``subprocess.run(["bash", "-c", 'printf ... > /outside/file'])`` wrote OUTSIDE
working_root with rc=0 -- a spawned child is an OS-level process and no Python-side
file patch reaches it. The whole class of "spawn something that does the file work"
is now closed at RUNTIME, for every profile:

* SHELLS are never spawnable (Windows + POSIX): cmd/cmd.exe/powershell/pwsh/
  wscript/cscript/conhost and sh/bash/zsh/dash/ksh -- matched by basename of argv[0]
  ("cmd /c ..." string form, ["bash","-c",...] list form, "/bin/sh" absolute path).
* INTERPRETERS are never spawnable (python/python3/.../node/perl/ruby/php/java): a
  spawned interpreter is an unsandboxed second Python -- the same escape with extra
  steps.
* NETWORK transfer tools are never spawnable (curl/wget/ftp/lftp/sftp/scp/rsync/
  tftp/telnet): their whole purpose moves bytes OUT of the machine; use the
  dedicated download_file tool instead.
* SHELL-CONTEXT operator rule: whenever a command will be interpreted by a shell --
  explicit shell=True (always fails, see below), or ANY string-form Popen on Windows
  ``comspec /c "..."`` before CreateProcess) -- the command must contain NO shell
  operators (| & ; < > ( ) { } $ ` \\ newline). Quoted regions are respected:
  ``git commit -m "fix: a | b"`` is fine, ``echo hi; rm -rf /`` is not.
* os.system: same policy as string-form shell context, enforced by patching the
  function (the audit hook alone cannot block it -- hooks observe, they do not veto).
* shell=True therefore ALWAYS fails on both platforms -- the shell binary itself
  appears as argv[0] in the audited command ('/bin/sh' or ComSpec/cmd.exe), which is
  on the denylist. String-form commands WITHOUT operators still work on Windows
  (CPython wraps them in `comspec /c "..."`); list-form with a clean program name
  works everywhere.
* os.system / os.popen: bare commands only -- no shell operators, no denied programs.
  Policy-based rather than blanket-deny because on POSIX os.system is implemented via
  os.popen internally (a blanket deny would break legitimate os.system() on macOS).
* os.spawn*: allowed only for executables that are NOT on the denylist above.
* os.exec*: DENIED outright -- they REPLACE this process, which would kill the
  sandboxed child and hand control to an unsandboxed one (POSIX-only; no-op elsewhere).
* os.fork: if it returns 0 we ARE the child -> immediate os._exit(97) before any user
  code runs there. A forked child inherits patched builtins but could still exec() in
  C; killing it at the Python level removes that vector (POSIX-only).
* socket connect-family: connect, connect_ex, sendto, sendall, sendmsg and
  create_connection are LOOPBACK ONLY by default (2026-09-15; extended 2026-09-16 after
  probes proved the .connect-only patch left unconnected-UDP sendto and
  connect_ex open); sendfile is denied outright (local-file exfiltration). Localhost
  probes (health checks against a local service) keep working. OUT-OF-SANDBOX NETWORK
  CONSENT GATE (2026-11-24): attempting a non-loopback target now ASKS THE LOCAL USER --
  the child writes .temp/.jack_netreq/<id>.json and waits up to JACK_PYEXEC_NET_ASK_TIMEOUT
  seconds for the parent's decision file; python_exec.py relays each request to the UI as
  an approval card (Allow this connection / Deny). No answer in time -> deny (fail-closed,
  same outcome as before the gate existed); decisions are cached per target for the run.
  (2026-09-30): when the calling process's environment carries JACK_PYEXEC_NETWORK=1,
  python_exec.py forwards it to the child and this stage installs NO socket
  restrictions at all -- admins get full outbound network inside python_exec. The
  default stays loopback-only (fail-closed).
* IMPORT BLOCKS (child level): ctypes/_ctypes/cffi (native-code file APIs bypass every
  path check), multiprocessing + its submodules (spawned workers are unsandboxed OS
  processes; on macOS the DEFAULT start method is "spawn" -- verified escape class),
  pty (spawns a login shell directly), webbrowser (launches external programs with an
  attacker-controlled URL), venv/ensurepip (interpreter installation outside root),
  nt/_winapi/winreg (raw Win32 file/registry APIs; added 2026-09-17 -- see HARDENING).

ENFORCEMENT MECHANISM (verified empirically on CPython 3.12, Windows; portable by construction)
=====================================================================
Primary: ``sys.addaudithook`` -- the "open" event fires for builtins.open AND
io.FileIO; raising PermissionError inside the hook blocks the operation and delivers
our message to user code. Audited os events (listdir/scandir/walk/mkdir/rename/...) are
checked too, as a second net over their patched counterparts. The "import" event
blocks the modules listed above before any of their code loads. The "subprocess.Popen"
event fires for EVERY spawn path -- subprocess.run/Popen, asyncio.create_subprocess_*,
pty (before its import block would stop it), popen2/os.system internals -- so the exec
policy is enforced at one choke point; os.* patches + a Popen.__init__ patch are second nets.

AUDIT-ARGS SHAPE NOTE (CPython 3.12, verified against Lib/subprocess.py source):
* Windows: args = (None|executable_str, list2cmdline(args) or raw str, cwd, env).
  The program is the FIRST QUOTE-AWARE TOKEN of that string; shell=True shows up as
  executable == ComSpec/cmd.exe.
* POSIX:   args = (args[0] if no executable else executable, [arglist], cwd, env);
  with shell=True the list becomes ['/bin/sh', '-c', <user-string>].

Secondary: patches for primitives WITHOUT an audited event in 3.12 (builtins.open --
defense-in-depth on top of the audit hook; os.open -- flag-aware; mutation ops
remove/unlink/rename/replace/rmdir/mkdir/makedirs/symlink/link/chmod/chown/utime/
truncate) plus sqlite3.connect path validation, including its C-VFS and 'file:...' URI
bypasses (audit 2026-08-23, F4). Two-path write ops (rename/replace/link/symlink)
validate EVERY path-like positional argument -- auditing only the first was a verified
escape (audit 2026-08-31, finding F1).

FAIL-CLOSED CONTRACT
====================
If ``JACK_PYEXEC_ROOT`` is missing/invalid the bootstrap prints to stderr and calls
``os._exit(3)`` (a C-level exit that cannot be intercepted by user code or a patched
builtin) BEFORE any user code runs. python_exec.py maps rc==3 to a friendly message.

DELIVERY NOTE (CLIENT sandbox)
==============================
Data-only, zero imports at module level (the string itself imports os/sys when it is
executed in the child). Shipped as a same-package dependency of python_exec.py exactly
like _policy.py: tool_scanner._find_same_package_imports() picks up the
'from ._sandbox_bootstrap import SANDBOX_BOOTSTRAP' line and dynamic_loader injects
the symbol. The bootstrap string is plain data, so it survives every delivery context
(server import / CLIENT exec / standalone) untouched.

EMBEDDED LITERAL (2026-09-23 fix): SANDBOX_BOOTSTRAP below is an IN-FILE r''' literal --
the module performs NO disk reads at import time. The 2026-09-18 split made this
module read the _sandbox_parts/ data files on import; that broke CLIENT delivery,
where the dep source is exec'd in-memory (no __file__, cwd = user's working_root)
and no such folder exists -> FileNotFoundError killed python_exec on every client.
_sandbox_parts/ remains ONLY as an auditable provenance view of this literal
(regenerate with tools/exec_tools/_regen_sandbox_parts.py; the test suite pins them
to the literal byte-for-byte).

RESIDUAL LIMITATIONS (documented -- same class as any path sandbox)
===================================================================
* Raw fd operations on an ALREADY-OPEN file object (f.read/f.write, os.read,
  os.write) cannot be re-checked: the open itself was scoped, so no NEW outside
  content can enter through them -- but a legal in-root fd is opaque afterwards.
* SourceFileLoader.exec_module (loading an OUTSIDE .py via importlib directly,
  no "import" statement) is NOT separately guarded: its source read goes
  through io.open and hits the "open" audit check, so it stays contained.
* Device names (Windows nul/COM1..9, /dev/null) are exempt from path checks --
  they are not filesystem paths; subprocess.DEVNULL depends on this.
* File-descriptor-level I/O after a legal open cannot be re-checked (fd is opaque).
  Mitigated because fds can only be created through the checked primitives above.
* Bare allowed external commands (git, npm, cargo, ...) run at OS level: they cannot
  touch files outside working_root without going through their own network/remote
  features (e.g. `git push` to a remote). Filesystem containment of the child process
  itself is total; command-level behavior is the responsibility of that tool.
* winreg import is blocked in the child (2026-09-17); registry access from ordinary
  python_exec code is therefore unavailable on builds where it is not preloaded. On
  CPython 3.12 Windows it IS preloaded -- same class as nt/_winapi, and the raw file
  functions there are patched directly (fix #3 above).
* Setup-stage failures no longer fail open SILENTLY: any exception swallowed during
  bootstrap setup is appended to <working_root>/.temp/.jack_pyexec_sandbox_errors.txt so a
  broken guard can be spotted immediately (2026-09-17). Since 2026-11-14 a failed stage is
  also FATAL: _jps_fail() logs it and exits 3 before user code runs -- a guard that did not
  install is treated as no sandbox at all.
* RESIDUAL (2026-11-14): raw _winapi.CreateProcess with executable=None is DENIED unless this
  run's subprocess.Popen event just authorised a spawn (single-use token). The audit event for
  CreateProcess does not expose the COMMAND LINE, so the hook cannot police a smuggled
  command line by itself; legitimate spawns pass because subprocess audits Popen.__init__
  before CreateProcess, and the stage-6 wrapper polices the command line for calls that go
  through the patched function. A payload that extracts the ORIGINAL CreateProcess from the
  wrapper's closure with executable=None is therefore blocked; with an explicit executable
  path it is policed at the audit level (executable containment + program denylist), but its
  command-line arguments are not inspected on that route.
* RESIDUAL (2026-11-14): the consent-decision lock covers DECISION files. A payload can still
  write its own REQUEST file under .temp/.jack_netreq (that is the documented channel) and can
  read decisions; it cannot answer itself. The parent must keep writing decisions OUTSIDE the
  child's writable tree or on a channel the child cannot write -- this layer only makes the
  in-root decision path unwritable from inside the child.
* RESIDUAL (2026-11-15): a renamed shell whose basename is not an EXACT denylist name
  ('mysh.exe', 'myash.exe') is not caught -- 'sh'/'ash' are exact basenames only, because
  as suffixes they denied legitimate programs (finish/flash/push/wash, verified). Renamed
  copies of cmd/powershell/pwsh/wscript/cscript/conhost/bash/zsh/dash/ksh/csh/tcsh stay
  denied by suffix, and every spawnable program is still subject to the cwd + absolute
  argument rules.
* RESIDUAL (2026-11-14): spawned bare commands (git, npm, cargo, ...) still run at OS level;
  their absolute path ARGUMENTS are now validated, but a command that resolves files through
  its own internal logic (config paths, env vars, remote protocols) is that tool's behaviour,
  not something a Python-level sandbox can observe.
* PRE-EXISTING quirk (verified identical before and after 2026-11-14): on Windows every
  string-form command is shell-wrapped, so the shell-OPERATOR rule inspects its tokens --
  and '\' is itself an operator character. Any ABSOLUTE Windows executable path therefore
  contains an operator token and is DENIED (e.g. subprocess.run([shutil.which("git")]) ->
  'C:\Program Files\Git\cmd\git.EXE'); bare program names ('git') work. Left as-is:
  relaxing the rule for path-shaped tokens would weaken the check that blocks chained
  commands, and it is not a containment hole (it denies MORE, never less).
"""

SANDBOX_BOOTSTRAP = r'''
# === JACK python_exec runtime sandbox (auto-prepended; do not edit) ===
import os as _jps_os, sys as _jps_sys


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
            # Separator-anchored prefix, rstrip'd so a drive root ('C:\') does not
            # become 'C:\\' + sep (which would deny every child of that drive).
            # Same semantics as tools/path_guard.py::_is_path_inside_allowed.
            prefix = r.rstrip(_jps_os.sep) + _jps_os.sep
            if rp == r or rp.startswith(prefix):
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

    # Named-pipe / device namespace exemption (2026-09-17; NARROWED 2026-11-14): asyncio
    # subprocess transports create stdio pipes via _winapi.CreateFile on '\\.\pipe\...'
    # names, which are NOT filesystem paths, so the PIPE namespace must stay exempt.
    # The old rule exempted the WHOLE \\.\ namespace, which also covers raw volume and
    # device handles (\\.\PHYSICALDRIVE0, \\.\HarddiskVolume1, ...) -- real OS objects
    # outside containment. Now only \\.\pipe\* plus the classic console device names
    # (nul/con/prn/aux/com1..9, also handled by _is_devnull) are exempt.
    def _is_dev_or_pipe(p):
        s = p.decode("utf-8", "replace") if isinstance(p, (bytes, bytearray)) else str(p)
        b = s.strip().lower().replace("/", "\\")
        if b.startswith('\\\\.\\pipe\\') or b.startswith('//./pipe/'):
            return True
        return b in ("nul", "con", "prn", "aux", "com1", "com2", "com3", "com4",
                     "com5", "com6", "com7", "com8", "com9")

    # Path-string helper used by the audit hook AND the raw-Win32 wrappers (defined here,
    # before both, so the hook never hits a NameError on a non-Windows build where the
    # stage-6 block does not run).
    def _path_str(x):
        """Best-effort path string (str/bytes/os.PathLike); None otherwise."""
        if isinstance(x, bytes):
            return x.decode("utf-8", "replace")
        if isinstance(x, str):
            return x
        try:
            return _jps_os.fspath(x)
        except Exception:
            return None

    # Write policy for _winapi.CreateFile (Windows), used by the AUDIT hook AND the stage-6
    # wrapper. Two independent signals decide "write":
    #   * the ACCESS mask contains any write bit -- GENERIC_WRITE (0x40000000),
    #     FILE_GENERIC_WRITE (0x120116), DELETE (0x00010000);
    #   * the CREATION DISPOSITION creates or truncates -- CREATE_NEW (1),
    #     CREATE_ALWAYS (2), TRUNCATE_EXISTING (5).
    # Reading only the access mask was a verified miss (2026-11-14): FILE_GENERIC_WRITE
    # contains DELETE but no GENERIC_WRITE bit, and the disposition slot was never looked
    # at, so a write handle opened with a create disposition slipped past the check.
    # _winapi exposes only OPEN_EXISTING/GENERIC_*/FILE_GENERIC_*, so the disposition and
    # DELETE values are Win32 API constants (stable across versions).
    _WACC_MASK = 0
    _WCREATIONS = frozenset((1, 2, 5))  # CREATE_NEW, CREATE_ALWAYS, TRUNCATE_EXISTING
    if _jps_os.name == "nt":
        _wapi_pre = _jps_sys.modules.get("_winapi")
        for _c, _fallback in (("GENERIC_WRITE", 0x40000000), ("FILE_GENERIC_WRITE", 0x120116)):
            try:
                _WACC_MASK |= getattr(_wapi_pre, _c)
            except Exception:
                _WACC_MASK |= _fallback
        _WACC_MASK |= 0x00010000  # DELETE access

    def _winapi_is_write(access, creation):
        if isinstance(access, int) and (access & _WACC_MASK) != 0:
            return True
        if isinstance(creation, int) and creation in _WCREATIONS:
            return True
        # Undetermined access AND undetermined disposition -> fail closed as a write.
        return not isinstance(access, int) and not isinstance(creation, int)

    # CONSENT-DECISION WRITE LOCK (2026-11-14, verified escape). The three out-of-sandbox
    # consent gates (network / file / subprocess) read the parent's answer from
    # <root>/.temp/.jack_netreq/<id>.decision.json -- a folder INSIDE working_root, which
    # the child can write. Without this lock a thread inside the payload wrote its own
    # {"decision":"allow"} and socket.connect + open(outside) both passed the gate.
    # Decision files are PARENT-OWNED: any write to one is denied outright, and never
    # asked (asking the user about the ask would be circular). Reads stay legal -- the
    # gates themselves read them. Covers every write route: open/flags, os.open, nt.open,
    # _winapi.CreateFile, and mutation ops that could REPLACE a decision file.
    def _is_consent_decision(p):
        s = _path_str(p)
        if s is None:
            return False
        b = s.strip().lower().replace("/", "\\")
        # The folder is '.jack_netreq' (leading dot) -- match the directory component,
        # not a bare '\jack_netreq\' which the dotted name does not contain.
        parts = b.split("\\")
        base = parts[-1]
        parent = parts[-2] if len(parts) >= 2 else ""
        return base.endswith(".decision.json") and parent == ".jack_netreq"

    def _deny_decision_write(what, p):
        raise PermissionError(
            "python_exec sandbox: %s of '%s' is blocked -- consent DECISION files under "
            ".temp/.jack_netreq are written by the parent process only. Code running "
            "inside python_exec must not answer its own approval request (fail-closed)."
            % (what, p)
        )

    # FAIL-CLOSED CONTRACT for setup stages (2026-11-14). Every containment stage used to
    # swallow its exception and continue: a child whose audit hook or patch stage failed to
    # install ran FULLY UNSANDBOXED with rc=0 (verified -- addaudithook raising left
    # os.listdir/open outside the root wide open). A guard that did not install is a
    # security failure, not a warning: log it, then exit 3 before any user code runs.
    def _jps_fail(where):
        _jps_log_exc(where)
        try:
            print("python_exec sandbox INIT FAILED: %s -- containment could not be "
                  "installed; refusing to run unsandboxed (fail-closed)." % where,
                  file=_jps_sys.stderr, flush=True)
        except Exception:
            pass
        _jps_os._exit(3)

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
        "sh", "bash", "zsh", "dash", "ksh", "ash", "tcsh", "csh", "fish",
        "ssh", "slogin",
        "python", "python2", "python3", "pythonw", "py", "node", "nodejs",
        "perl", "ruby", "php", "java",
        "curl", "wget", "ftp", "lftp", "sftp", "scp", "rsync", "tftp", "telnet",
        # WINDOWS SYSTEM-ABUSE UTILITIES (2026-11-15, verified gap). None of these is a
        # shell, so _SHELL_SUFFIXES never saw them and the sandbox let them run:
        #   wsl       -> the Linux side of the machine, no audit hook there at all: a full
        #                escape (filesystem, network, interpreter) with one argument.
        #   reg       -> registry WRITE (registry is not a filesystem path, so no path
        #                check can ever cover it).
        #   sc        -> service start/stop;  taskkill/tasklist -> process control/enumeration
        #   mshta     -> HTML Application host: runs script code outside this process
        #   certutil  -> decode/encode + download;  bitsadmin -> background transfer
        #   msiexec   -> installer;  rundll32 -> arbitrary DLL entry point
        #   net/net1  -> user/share/service administration;  netsh -> firewall config
        # Verified ALLOWED before this fix: wsl, reg, certutil, mshta, net, sc, rundll32,
        # tasklist. (bitsadmin/schtasks/taskkill/msiexec/netsh were blocked only by
        # accident -- see the switch-argument rule in _check_cmd_paths below.)
        "wsl", "reg", "bitsadmin", "certutil", "mshta", "net", "net1", "netsh",
        "schtasks", "taskkill", "tasklist", "sc", "msiexec", "rundll32",
    ))
    # Shell operators that let one string chain/redirect into arbitrary commands.
    _SHELL_OPS = frozenset("|&;<>(){}$`\\\n")

    def _deny_exec(what, detail):
        raise PermissionError(
            "python_exec sandbox: child-process spawn blocked -- %s (%s). "
            "Shells, interpreters, network tools and Windows system-abuse utilities "
            "cannot be spawned from python_exec; filesystem access stays inside "
            "working_root." % (what, str(detail)[:120])
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

    # Renamed-shell detection (2026-11-14, verified escape): cmd.exe copied to
    # 'mycmd.exe' ran '/c echo hi' with rc=0 -- the exact-name denylist never saw 'cmd'.
    # A shell binary renamed by the payload is still recognisable by its SUFFIX, so shell
    # stems are matched as suffixes too. Suffix (not substring) on purpose: 'copy'/'py'
    # style names must not false-positive.
    #
    # 'sh'/'ash' are deliberately NOT suffixes (2026-11-15, verified false positives): they
    # are substrings of ordinary program names, so the old rule DENIED finish, flash, push,
    # wash, brush, gosh, relish and notesh -- any tool whose name happens to end in 'sh'.
    # Real shells and ssh/fish/slogin are matched as EXACT basenames in _DENY_PROGS above,
    # which is the whole set of programs that actually exist as shells on these platforms.
    # The remaining residual is documented in the module docstring: a renamed shell whose
    # basename is not an exact match ('mysh.exe') is not caught here.
    _SHELL_SUFFIXES = ("cmd", "cmd.exe", "powershell", "pwsh", "wscript", "cscript",
                       "conhost", "bash", "zsh", "dash", "ksh", "csh", "tcsh")

    def _is_denied_prog(base):
        b = base[:-4] if base.endswith(".exe") else base
        if base in _DENY_PROGS or b in _DENY_PROGS or _is_interpreter(b):
            return True
        return any(b.endswith(s) for s in _SHELL_SUFFIXES)

    def _check_prog_base(base, ctx):
        if base and _is_denied_prog(base):
            # (2026-10-24) consentable: asks the local user when a UI relay is live.
            _exec_gate("program %r is a shell/interpreter/network/system tool" % base, ctx)

    # ARGUMENT PATH POLICY (2026-11-14, verified escape). The exec policy used to inspect
    # ONLY argv[0]'s basename, so arguments were completely unchecked: `findstr localhost
    # C:\Windows\System32\drivers\etc\hosts` printed the system HOSTS file (rc=0), and a
    # copy of cmd.exe renamed to mycmd.exe ran `/c echo hi` (rc=0) -- the denylist never
    # saw 'cmd'. Now every ABSOLUTE path argument a spawned program is given must resolve
    # inside working_root or a legitimate read root (Python home / sys.path / PATH dir);
    # anything else is consentable, exactly like an out-of-sandbox file open. Relative
    # args resolve against the child's cwd (= working_root), so they stay inside by
    # construction -- no false positives on URLs, git refs, regexes or bare words.
    def _check_cmd_paths(seq, ctx):
        for tok in seq[1:]:
            if not isinstance(tok, str) or not tok:
                continue
            if not _jps_os.path.isabs(tok):
                continue
            # Windows SWITCHES are drive-relative to isabs(): "/list", "/query", "/c:x",
            # "/f", "/m" all answer True (ntpath.isabs("/list") == True), so the rule above
            # misread them as ABSOLUTE PATH arguments and denied them -- verified false
            # positives on the pre-fix bootstrap: `sort /r`, `findstr /c:x file`,
            # `findstr /m def README.md`. A switch is a leading separator followed by an
            # ALPHA-ONLY flag, optionally ':value' (the value may contain dots/digits:
            # '/c:foo.txt', '/d:2024-01-01'); anything with a further separator, a dot in
            # the flag itself ('/win.ini' -> drive-relative C:\win.ini, a real file) or a
            # drive/UNC prefix is a path argument and stays checked. Windows-only: on POSIX
            # "/tmp" IS an absolute path and must stay checked.
            if _IS_WIN and tok[0] in ("/", "\\"):
                _body = tok[1:]
                if "/" not in _body and "\\" not in _body and _body.split(":", 1)[0].isalpha():
                    continue
            if _is_devnull(tok) or _is_dev_or_pipe(tok):
                continue
            if _inside(tok, READ_ROOTS) or _in_path_dirs(tok):
                continue
            _exec_gate("absolute path argument outside working_root", ctx)

    # CWD POLICY (2026-11-14, verified escape). Every spawn route accepts cwd=, and CPython
    # passes it to the audit event as args[2] of "subprocess.Popen" (Lib/subprocess.py:1534
    # Windows, :1823 POSIX) -- but the exec policy inspected only the COMMAND, so a spawn
    # with cwd outside working_root ran the grandchild THERE. The grandchild is an OS-level
    # unsandboxed process, and _check_cmd_paths() cannot see this: with cwd=C:\Windows the
    # arguments are RELATIVE, so no absolute path appears in argv at all. Verified live:
    #   subprocess.run(["findstr","16-bit","win.ini"], cwd=r"C:\Windows") -> rc=0, printed
    #   '; for 16-bit app support'  (content of C:\Windows\win.ini)
    #   subprocess.run("findstr 16-bit win.ini", cwd=r"C:\Windows")       -> rc=0, same leak
    # So cwd is a containment decision, not a cosmetic argument: it must resolve inside
    # working_root. Relative cwd strings resolve against the child's cwd (= working_root),
    # so they stay inside by construction; an earlier os.chdir() to an outside directory
    # makes a relative cwd resolve OUTSIDE and _norm() (realpath) catches that. An int cwd
    # is a file descriptor (os.fsdecode keeps it as an int) -- fds can only be created
    # through the checked open primitives above, so it passes through like elsewhere here.
    # Askable through the FILE consent gate (_fs_deny): the card shows the exact directory.
    def _check_cwd(cwd):
        if cwd is None:
            # No explicit cwd: the grandchild INHERITS this process's cwd, which user code
            # may have moved outside working_root with os.chdir() -- the same escape with
            # one step removed. Check the inherited value too (os.getcwd() fires no audit
            # event, so it is safe to call from the hook).
            try:
                cwd = _jps_os.getcwd()
            except Exception:
                cwd = ""  # undeterminable -> _inside() answers False -> fail closed
        s = _path_str(cwd)
        if s is None:
            return  # int fd / exotic shape -> let CPython handle it (fd came from a checked open)
        if _is_devnull(s) or _is_dev_or_pipe(s):
            return
        if not _inside(s, [ROOT]):
            _fs_deny("subprocess cwd", s)

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
            _check_cmd_paths(toks, cmdline)
        else:
            seq = [t.decode("utf-8", "replace") if isinstance(t, (bytes, bytearray)) else str(t)
                   for t in cmdline]
            prog = seq[0] if seq else ""
            _check_prog_base(_jps_basename(prog), cmdline)
            _check_cmd_paths(seq, cmdline)

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

    # ONE-SPAWN TOKEN (2026-11-14): the audit args of _winapi.CreateProcess do not expose
    # the command line (verified: arg1 is a 1-char internal marker), so a raw
    # CreateProcess(executable=None) call cannot be inspected at the audit level.
    # subprocess is the legitimate route: its Popen event and patched __init__ run BEFORE
    # CreateProcess. Each authorised Popen therefore issues a SINGLE-USE token that the
    # CreateProcess(exe=None) branch consumes -- a raw smuggled call with no preceding
    # authorised spawn finds no token and is denied, and a second smuggled call cannot
    # piggyback on an already-consumed one.
    _SPAWN_TOKEN = [0]

    def _spawn_authorized():
        if _SPAWN_TOKEN[0]:
            _SPAWN_TOKEN[0] = 0  # consume -- one CreateProcess per authorised Popen
            return True
        return False

    def _grant_spawn():
        _SPAWN_TOKEN[0] += 1

    # ---- 1. AUDIT HOOK: primary runtime enforcement -------------------------
    # FAIL-CLOSED ON INTERNAL ERRORS (2026-11-14): the hook used to swallow every
    # non-PermissionError, so a broken/patched containment check silently ALLOWED the
    # operation. Now: an error while handling an event this sandbox POLICES denies that
    # operation (fail closed); an unpoliced event still passes through, because CPython
    # fires many events the sandbox does not check and denying them would break normal
    # runs. _POLICED_EVENTS lists exactly the branches below.
    _POLICED_EVENTS = frozenset((
        "open", "os.listdir", "os.scandir", "os.walk",
        "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.rmdir",
        "os.chmod", "os.utime", "os.truncate", "os.link", "os.symlink",
        "shutil.copyfile", "shutil.move", "import", "subprocess.Popen",
        "os.system", "os.spawn",
        "_winapi.CreateFile", "_winapi.CreateProcess",
    ))

    def _audit(event, args):
        _policed = event in _POLICED_EVENTS
        try:
            if event == "open":
                target = args[0]
                if isinstance(target, int):          # fd-based open -> not a path
                    return
                s = (target.decode("utf-8", "replace")
                     if isinstance(target, bytes) else str(target))
                # Two audit shapes (verified on CPython 3.12): builtins.open / io.FileIO
                # carry (path, mode STRING, mode_int); os.open / nt.open carry
                # (path, None, FLAGS_INT) -- the mode slot is None and the flag word is
                # args[2]. Reading ONLY args[1] made every os.open/nt.open WRITE look like
                # a READ at the hook level (verified: os.open(p, O_WRONLY|O_CREAT) fired
                # ('open', (p, None, 385)) and the hook computed is_write=False), so the
                # write policy and the consent-decision lock were both bypassable there.
                _m = args[1] if len(args) > 1 else None
                _fl = args[2] if len(args) > 2 else None
                _wf = 0
                for _f in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
                    try:
                        _wf |= getattr(_jps_os, _f)
                    except Exception:
                        pass
                if isinstance(_m, int):
                    is_write = bool(_m & _wf)
                elif isinstance(_fl, int):
                    is_write = bool(_fl & _wf)
                else:
                    is_write = any(ch in str(_m or "r") for ch in "wax+")
                if _is_devnull(s) or _is_dev_or_pipe(s):
                    return  # device name (nul /dev/null) -- not a filesystem path
                if is_write and _is_consent_decision(s):
                    # CONSENT SELF-APPROVAL LOCK (2026-11-14, verified escape): a thread
                    # inside the payload wrote its own <id>.decision.json and the gate read
                    # it as the user's answer. Decision files are PARENT-OWNED -> hard deny,
                    # never askable (asking about the ask would be circular).
                    _deny_decision_write("open(write)", s)
                roots = [ROOT] if is_write else READ_ROOTS
                if not _inside(s, roots):
                    _fs_deny("open" + ("(write)" if is_write else ""), s)
            elif event in ("_winapi.CreateFile",):
                # Raw Win32 handle factory, checked at the AUDIT level (2026-11-14): the
                # function wrapper below is bypassable by pulling the ORIGINAL builtin out
                # of its closure cells (verified: __closure__ hands back the pre-patch
                # CreateFile). The audit event fires inside the C function, so it holds no
                # matter how the call was reached. args = (name, access, share, creation...)
                nm = _path_str(args[0]) if args else None
                if nm is not None and _is_dev_or_pipe(nm):
                    return  # \\.\pipe\* stdio pipes -- not filesystem paths
                if nm is not None:
                    acc = args[1] if len(args) > 1 else None
                    # Verified audit shape on 3.12: (name, access, share, CREATION, flags).
                    creation = args[3] if len(args) > 3 else None
                    if _winapi_is_write(acc, creation):
                        if _is_consent_decision(nm):
                            _deny_decision_write("_winapi.CreateFile(write)", nm)
                        if not _inside(nm, [ROOT]):
                            _fs_deny("_winapi.CreateFile(write)", nm)
                    elif not _inside(nm, READ_ROOTS):
                        _fs_deny("_winapi.CreateFile(read)", nm)
            elif event == "_winapi.CreateProcess":
                # Raw spawn (2026-11-14, verified leak): a direct _winapi.CreateProcess
                # call bypasses subprocess, so the Popen event never fires for it.
                # Audit args on CPython 3.12 are (executable, <internal marker>, cwd) --
                # verified live: (None, '\x05', 'C:\\Windows'). The COMMAND LINE is NOT
                # exposed (arg1 is a 1-char marker, not the string we passed), so the audit
                # level can police the EXECUTABLE and the CWD only; command-line arguments
                # are policed by the wrapper patch in stage 6. cwd is checked FIRST here --
                # it holds for every route (subprocess, asyncio, raw calls, closure-extracted
                # originals), which is exactly what the stage-6 wrapper cannot guarantee.
                _check_cwd(args[2] if len(args) > 2 else None)
                exe = _path_str(args[0]) if args else None
                if exe is None:
                    if not _spawn_authorized():
                        raise PermissionError(
                            "python_exec sandbox: _winapi.CreateProcess with executable=None "
                            "is blocked -- the program lives in the command line, which the "
                            "audit event cannot inspect. Use subprocess.run/Popen (its exec "
                            "policy is checked)."
                        )
                    return
                if not _inside(exe, READ_ROOTS) and not _in_path_dirs(exe):
                    _fs_deny("_winapi.CreateProcess (executable)", exe)
                _check_prog_base(_jps_basename(exe), args)
            elif event in ("os.listdir", "os.scandir", "os.walk"):
                # Enumeration: read-side -> working_root + Python home.
                # _path_str() DECODES bytes (verified escape 2026-11-14): str(args[0]) on a
                # bytes path yields "b'C:\\Windows'", which _norm()/realpath() resolves as a
                # NON-EXISTENT child OF THE ROOT -> _inside() answered True -> the check
                # passed and os.listdir(b"C:\\Windows") / os.scandir(b"...") / os.walk(b"...")
                # enumerated outside working_root with no denial and no consent request.
                # Bytes is a legal path type (os.listdir accepts str/bytes/PathLike), and the
                # nt.* wrappers below already decode via _as_path_str(), so the audit-level
                # branch must decode the same way -- exactly like the "open" branch does.
                if args and isinstance(args[0], int):
                    # Enumeration BY FILE DESCRIPTOR: the fd IS the directory being
                    # enumerated, so there is no path for the hook to validate. Pre-fix the
                    # branch denied it anyway (str(fd) resolves as a non-existent child of
                    # the root); kept fail-closed, but as a HARD block with an honest message
                    # -- an fd number is not a path, so neither a consent card nor the
                    # "outside working_root" wording makes sense here.
                    raise PermissionError(
                        "python_exec sandbox: %s by file descriptor (%d) is blocked -- the "
                        "hook sees no path to validate. Enumerate by path instead."
                        % (event, args[0])
                    )
                s = _path_str(args[0])
                if s is None:
                    # NO PATH ARGUMENT: os.listdir()/os.scandir() take the path from the
                    # C level, so the audit event carries args[0] = None (verified). os.py
                    # binds os.listdir to the ORIGINAL nt.listdir object at import time
                    # (`from nt import *`), so the stage-6 nt.listdir patch cannot reach the
                    # zero-argument call -- the hook is the ONLY layer on that route. The old
                    # str(None) == "None" resolved as <root>\None, which is NOT a read root,
                    # so every default enumeration was DENIED -- including legitimate
                    # os.listdir() inside working_root (false positive, verified). It never
                    # leaked, but containment there rested on a coincidence rather than on the
                    # real directory. Resolve the inherited cwd instead (same rule as
                    # _check_cwd()): in-root default enumeration works, a cwd moved outside
                    # working_root by os.chdir() stays denied.
                    try:
                        s = _jps_os.getcwd()
                    except Exception:
                        s = ""  # undeterminable -> _inside() answers False -> fail closed
                if not _inside(s, READ_ROOTS):
                    _fs_deny(event + " (enumeration)", s)
            elif event in ("os.remove", "os.unlink", "os.rename", "os.replace",
                           "os.mkdir", "os.rmdir", "os.chmod", "os.utime",
                           "os.truncate", "os.link", "os.symlink"):
                # MUTATING ops: validate EVERY path-like positional arg (audit
                # 2026-09-16). These fire audit events in CPython core, so this
                # check holds even if user code extracts the ORIGINAL function from
                # a wrapper closure and calls it directly -- verified bypass class:
                # "builtins.open.__closure__" / "_wrap_write" cells. Shapes observed on
                # 3.12: os.rename/os.link carry (src, dst, ...), everything else has
                # the path first; ints/sentinels are skipped by the isinstance filter.
                for _a in args[:4]:
                    if isinstance(_a, (str, bytes)):
                        _s = _a.decode("utf-8", "replace") if isinstance(_a, bytes) else str(_a)
                        if _is_consent_decision(_s):
                            # A mutation can REPLACE a decision file (os.rename / os.replace
                            # / shutil.move onto it) -- same self-approval escape as an
                            # open(w), so it is denied outright.
                            _deny_decision_write(event, _s)
                        if not _inside(_s, [ROOT]):
                            _fs_deny(event + " (write)", _s)
            elif event in ("shutil.copyfile", "shutil.move"):
                # HIGH-LEVEL FILE FUNCTIONS (hardening 2026-09-17): CPython audits
                # these with (src, dst). copy2's Windows fast path (_winapi.CopyFile2)
                # and move()'s cross-drive fallback fire NO "open" event -- this is
                # the net that caught the verified other-drive escape. src: read-side;
                # dst: write-side (ROOT only). Non-path args are skipped.
                _src = args[0] if len(args) > 0 else None
                _dst = args[1] if len(args) > 1 else None
                for _a, _lbl in ((_src, "source"), (_dst, "destination")):
                    if isinstance(_a, (str, bytes)):
                        _s = _a.decode("utf-8", "replace") if isinstance(_a, bytes) else str(_a)
                        _roots = [ROOT] if _lbl == "destination" else READ_ROOTS
                        if not _inside(_s, _roots):
                            _fs_deny(event + " (" + _lbl + ")", _s)
            elif event == "import":
                name = str(args[0]).split(".")[0]
                if name in ("ctypes", "_ctypes", "cffi"):
                    _deny("import of native-code escape primitive", name)
                elif name in ("multiprocessing", "pty", "webbrowser", "venv", "ensurepip"):
                    raise PermissionError(
                        "python_exec sandbox: import of %r is blocked -- it can spawn "
                        "unsandboxed child processes or launch external programs." % name
                    )
                elif name in ("nt", "_winapi", "winreg"):
                    # Raw Win32 file/registry APIs (hardening 2026-09-17). NOTE: on
                    # CPython 3.12 Windows these modules are PRELOADED before this
                    # hook runs, so the block is a no-op there -- the raw functions
                    # themselves are patched directly in stage "RAW WIN32" below.
                    raise PermissionError(
                        "python_exec sandbox: import of %r is blocked -- raw OS-level "
                        "file/registry APIs bypass working_root containment." % name
                    )
            elif event in ("socket.connect", "socket.connect_ex", "socket.sendto"):
                # AUDIT-LEVEL outbound policy (2026-11-14): the socket wrappers below are
                # bypassable the same way the file wrappers are -- socket.connect.__closure__
                # hands back the pre-patch C method (verified: an extracted original
                # connected to a non-loopback target with no gate). These events fire inside
                # C, so the loopback rule and the consent gate hold however the call was
                # reached. args = (socket, address[, ...]). The admin opt-in
                # (JACK_PYEXEC_NETWORK=1 -> _NET_OPEN) installs NO restrictions, so the hook
                # honours it too -- resolved at call time like every other late name here.
                if _NET_OPEN or len(args) < 2:
                    return
                _net_gate(args[1], event.split(".", 1)[1])
            elif event == "subprocess.Popen":
                # Fires for EVERY spawn path (subprocess.run/Popen, asyncio
                # create_subprocess_*, popen2/os.system internals). Shape in 3.12:
                #   Windows: (None|exe_str, list2cmdline(args) or raw str, cwd, env)
                #             shell=True -> exe == ComSpec/cmd.exe (denied by basename)
                #   POSIX:   (args[0]|exe, [arglist], cwd, env); shell=True ->
                #             ['/bin/sh', '-c', <user-string>] (sh denied by basename)
                for a in args:
                    if isinstance(a, str):
                        _check_cmd(a)
                        break
                    if isinstance(a, (list, tuple)):
                        _check_cmd(list(a))
                        break
                # cwd is args[2] on BOTH platforms (verified against Lib/subprocess.py:
                # Windows sys.audit("subprocess.Popen", executable, args, cwd, env) at
                # line 1534, POSIX the same at line 1823). Checked AFTER the command so a
                # denied program still reports the exec-policy reason first.
                _check_cwd(args[2] if len(args) > 2 else None)
                # This spawn passed the exec policy -> let the ONE following raw
                # CreateProcess(executable=None) consume the token (see _SPAWN_TOKEN).
                _grant_spawn()
            elif event == "os.system":
                # os.system is patched below, but the audit event is the net that holds
                # if the wrapper is bypassed (closure-extracted original). args=(cmd,).
                # os.system is C-level -- it fires NO Popen/CreateProcess event (verified),
                # so the cwd rule must be applied here too: the grandchild inherits OUR
                # cwd, which os.chdir() may have moved outside working_root.
                if args and isinstance(args[0], str):
                    _check_cmd(args[0], shell_ctx=True)
                    _check_cwd(None)
            elif event == "os.spawn":
                # os.spawn* (mode, path, argv...). Patched below too; this is the net.
                # Same inherited-cwd rule as os.system (no Popen event on this route).
                if len(args) >= 2:
                    _check_prog_base(_jps_basename(_path_str(args[1]) or ""), args)
                    if len(args) >= 3 and isinstance(args[2], (list, tuple)):
                        _check_cmd_paths([str(x) for x in args[2]], args)
                    _check_cwd(None)
        except PermissionError:
            raise
        except Exception as _jps_hook_err:
            # FAIL CLOSED for policed events (2026-11-14): an internal error inside a
            # containment branch must NOT let the operation through. Unpoliced events
            # still pass -- they are not filesystem/spawn/import decisions.
            if _policed:
                _jps_log_exc("audit hook (%s): %s" % (event, repr(_jps_hook_err)))
                raise PermissionError(
                    "python_exec sandbox: %s blocked -- the containment check for this "
                    "operation failed (%s), so it was denied (fail-closed)."
                    % (event, repr(_jps_hook_err)[:120])
                )
            pass  # malformed/unpoliced event -- never fail-open unrelated CPython events

    try:
        _jps_sys.addaudithook(_audit)
    except Exception as _jps_hook_exc:
        # FAIL CLOSED (2026-11-14, verified): the hook is the PRIMARY enforcement layer --
        # enumeration, raw Win32 and spawn checks exist ONLY here. Swallowing this left the
        # child fully unsandboxed with rc=0. A hook that cannot install means no sandbox.
        _jps_fail("audit hook could not be installed")

    try:
        _jps_log("B1: audit hook installed (open/enumeration/import/Popen checks live)")
    except Exception:
        pass


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
                if _is_consent_decision(s):
                    # rename/replace/move onto a decision file = self-approval escape.
                    _deny_decision_write(nm, s)
                if not _inside(s, [ROOT]):
                    _fs_deny(nm + " (write)", s)
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

        # CONSENT FIX (2026-10-12): the metadata wrappers below used to answer "not found /
        # False" for EVERY outside path -- even ones the local user had already
        # approved via the OUT-OF-SANDBOX FILE CONSENT GATE (defined later in this
        # same function, resolved here at call time). That made os.stat/exists/isdir/
        # getsize LIE after an approval while open/listdir worked fine. _consent_ok()
        # consults the gate's decision cache: an outside path already allowed this run
        # (or under an approved parent dir) falls through to the original call; a deny,
        # timeout or unasked path stays fail-closed. Metadata probes never ASK -- only
        # data operations do -- so no dialog spam from existence checks.
        def _consent_ok(s):
            try:
                return _fs_cached(_norm(s)) == "allow"
            except Exception:
                return False  # cannot resolve / not decided -> fail closed

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
                if not _inside(s, roots) and not _consent_ok(s):
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
                    if s is not None and not _is_devnull(s) and not _inside(s, [ROOT]) and not _in_path_dirs(s) \
                        and not _consent_ok(s):
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
                        if not _inside(s, [ROOT]) and not _in_path_dirs(s) \
                                and not _consent_ok(s):
                            return False
                    return _orig_exists(p)
                _op.exists = _path_exists
            for _n in ("isfile", "isdir"):
                _orig_p = getattr(_op, _n)
                def _make_pred(orig_fn, nm):
                    def wrapped(p):
                        if isinstance(p, (str, bytes)):
                            s = p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p)
                            if not _inside(s, [ROOT]) and not _in_path_dirs(s) \
                                    and not _consent_ok(s):
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
                        if not _inside(s, [ROOT]) and not _consent_ok(s):
                            raise FileNotFoundError(
                                "[Errno 2] No such file or directory (outside working_root -- "
                                "python_exec sandbox blocks metadata access outside %s)" % root
                            )
                    return _orig_getsize(f)
                _op.getsize = _path_getsize
        except Exception as _jps_mod_exc:
            _jps_fail("os.path meta patch: %s" % repr(_jps_mod_exc))

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
                    if s is not None and not _inside(s, roots) and not _consent_ok(s):
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
                    if (s is not None and not _inside(s, [ROOT]) and not _in_path_dirs(s)
                            and not _consent_ok(s)):
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
                    if s is not None:
                        if _is_consent_decision(s):
                            _deny_decision_write("Path." + nm, s)
                        if not _inside(s, [ROOT]):
                            _fs_deny("Path." + nm + " (write)", s)
                    for _x in a:
                        xs = _path_str(_x)
                        if xs is not None:
                            if _is_consent_decision(xs):
                                _deny_decision_write("Path." + nm + " target", xs)
                            if not _inside(xs, [ROOT]):
                                _fs_deny("Path." + nm + " target (write)", xs)
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
                    _fs_deny("Path.symlink_to (write)", s)
                ts = _path_str(target)
                if ts is not None and not _inside(ts, READ_ROOTS):
                    _fs_deny("Path.symlink_to target", ts)
                return _orig_sym(self, target, *a, **k)
            _jps_Path.symlink_to = _symlink_to
        except Exception as _jps_mod_exc:
            _jps_fail("pathlib patch: %s" % repr(_jps_mod_exc))

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
            if is_write and _is_consent_decision(s):
                _deny_decision_write("os.open(write)", s)
            if not _inside(s, [ROOT] if is_write else READ_ROOTS):
                _fs_deny("os.open" + ("(write)" if is_write else ""), s)
            return _orig_open(path, flags, *a, **k)
        _real_os.open = _os_open

        try:
            _jps_log("B2e: os.open flag-aware patch installed")
        except Exception:
            pass

    except Exception as _jps_stage_exc:
        _jps_fail("stage 2 (os patches): %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B2: os/pathlib/os.open patching done")
    except Exception:
        pass


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
                _check_cmd_paths(seq, args)  # absolute-path args (2026-11-14)
            if executable is not None:
                _check_prog_base(
                    _jps_basename(executable.decode("utf-8", "replace")
                                  if isinstance(executable, bytes) else str(executable)),
                    "executable=" + repr(str(executable))[:60],
                )
            # cwd= (2026-11-14 containment): the wrapper binds args/bufsize/executable/
            # stdin/stdout/stderr explicitly, so the leftover positional tail is
            # (preexec_fn, close_fds, shell, cwd, env, ...) -- cwd is a[3]. Keyword form
            # is k["cwd"]. The wrapper sees the CALLER's own cwd value -- exactly what the
            # audit event cannot show for a caller that extracts the original __init__
            # from this wrapper's closure.
            _cwd = k.get("cwd") if "cwd" in k else (a[3] if len(a) > 3 else None)
            _check_cwd(_cwd)
            return _orig_popen_init(
                self, args, bufsize=bufsize, executable=executable,
                stdin=stdin, stdout=stdout, stderr=stderr, *a, **k
            )

        _jps_subprocess.Popen.__init__ = _popen_init
    except Exception as _jps_stage_exc:
        _jps_fail("stage 3 (subprocess patch): %s" % repr(_jps_stage_exc))

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
                        # (2026-10-24) consentable: asks the local user when a UI relay is live.
                        _exec_gate("shell operator in command (token %r)" % tok, cmd)
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
                        # (2026-10-24) consentable: asks the local user when a UI relay is live.
                        _exec_gate("shell operator in command (token %r)" % tok, cmd)
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
                            # (2026-10-24) consentable: asks the local user when a UI relay is live.
                            _exec_gate("%s of %r" % (nm, s), path)
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
        _jps_fail("stage 3b (os exec policy): %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B3: child-process exec policy done (shells/interpreters/network denied)")
    except Exception:
        pass


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
                        _fs_deny(nm + " (" + _lbl + ")", s)
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
                    if _lbl == "destination" and _is_consent_decision(s):
                        _deny_decision_write("shutil.move (destination)", s)
                    _roots = [ROOT] if _lbl == "destination" else READ_ROOTS
                    if not _inside(s, _roots):
                        _fs_deny("shutil.move (" + _lbl + ")", s)
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
                        _fs_deny("shutil.copytree (" + _lbl + ")", s)
                return _orig_ct(src, dst, *a, **k)
            _jps_shutil.copytree = _copytree

        if hasattr(_jps_shutil, "rmtree"):
            _orig_rmt = _jps_shutil.rmtree
            def _rmtree(path, *a, **k):
                s = (path.decode("utf-8", "replace")
                     if isinstance(path, bytes) else str(path))
                if not _inside(s, [ROOT]):
                    _fs_deny("shutil.rmtree (write)", s)
                return _orig_rmt(path, *a, **k)
            _jps_shutil.rmtree = _rmtree

        if hasattr(_jps_shutil, "make_archive"):
            _orig_ma = _jps_shutil.make_archive
            def _make_archive(base_name, *a, **k):
                s = (base_name.decode("utf-8", "replace")
                     if isinstance(base_name, bytes) else str(base_name))
                d = _jps_os.path.dirname(s) or "."
                if not (_inside(d, [ROOT]) and _inside(s, [ROOT])):
                    _fs_deny("shutil.make_archive (output path)", s)
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
                    _fs_deny("shutil.unpack_archive (archive)", s)
                if not _inside(ds, [ROOT]):
                    _fs_deny("shutil.unpack_archive (extract dir)", ds)
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
        _jps_fail("stage 4 (shutil): %s" % repr(_jps_stage_exc))

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
                    _fs_deny("zipfile.ZipFile" + ("(write)" if is_write else ""), s)
            return _orig_zf_init(self, file, mode, *a, **k)
        _jps_zipfile.ZipFile.__init__ = _zf_init

        _orig_extractall = _jps_zipfile.ZipFile.extractall
        def _extractall(self, path=None, members=None, pwd=None, *a, **k):
            p = path if path is not None else "."
            ps = (p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p))
            if not _inside(ps, [ROOT]):
                _fs_deny("zipfile extractall destination", ps)
            for m in (members or self.namelist()):
                _zip_member_ok(m)
            return _orig_extractall(self, path, members, pwd, *a, **k)
        _jps_zipfile.ZipFile.extractall = _extractall

        _orig_extract = _jps_zipfile.ZipFile.extract
        def _extract(self, member, path=None, pwd=None):
            p = path if path is not None else "."
            ps = (p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p))
            if not _inside(ps, [ROOT]):
                _fs_deny("zipfile extract destination", ps)
            _zip_member_ok(member)
            return _orig_extract(self, member, path, pwd)
        _jps_zipfile.ZipFile.extract = _extract

    except Exception as _jps_stage_exc:
        _jps_fail("stage 5 (zipfile): %s" % repr(_jps_stage_exc))

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
                    _fs_deny("tarfile.TarFile" + ("(write)" if is_write else ""), s)
            return _orig_tf_open(name, mode, *a, **k)
        _jps_tarfile.TarFile.open = _tf_open

        _orig_textractall = _jps_tarfile.TarFile.extractall
        def _textractall(self, path=None, members=None, *a, **k):
            p = path if path is not None else "."
            ps = (p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p))
            if not _inside(ps, [ROOT]):
                _fs_deny("tarfile extractall destination", ps)
            # ZIP uses `members or self.namelist()`; tar used `members or []`, which
            # validated NOTHING when members is None -- the DEFAULT case. A tar carrying
            # '../x' (or a deeper traversal) then extracted straight out of working_root
            # (verified). Validate every member of the archive when the caller did not
            # pass a list, exactly like zipfile does.
            checked = members if members is not None else self.getmembers()
            for m in checked:
                nm = m.name if hasattr(m, "name") else m
                _tar_member_ok(nm)
            # 'fully_trusted' disables CPython's own member filtering entirely; the
            # sandbox's own traversal check above still runs, but a blanket-trusted
            # extraction is never legitimate inside a containment sandbox -> deny.
            filt = k.get("filter") if "filter" in k else (a[0] if a and isinstance(a[0], str) else None)
            if filt == "fully_trusted":
                _deny("tarfile extractall filter='fully_trusted'", ps)
            return _orig_textractall(self, path, members, *a, **k)
        _jps_tarfile.TarFile.extractall = _textractall

        _orig_textract = _jps_tarfile.TarFile.extract
        def _textract(self, member, path=None, *a, **k):
            p = path if path is not None else "."
            ps = (p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p))
            if not _inside(ps, [ROOT]):
                _fs_deny("tarfile extract destination", ps)
            nm = member.name if hasattr(member, "name") else member
            _tar_member_ok(nm)
            return _orig_textract(self, member, path, *a, **k)
        _jps_tarfile.TarFile.extract = _textract

    except Exception as _jps_stage_exc:
        _jps_fail("stage 5b (tarfile): %s" % repr(_jps_stage_exc))

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
                                _fs_deny(nm + ".open" + ("(write)" if is_write else ""), s)
                        return orig_fn(filename, mode, *a, **k) if filename is not None \
                            else orig_fn(*a, **k)
                    wrapped.__name__ = nm
                    return wrapped
                setattr(_m, "open", _make_comp_guard(_orig_co, _modname))
        except Exception as _jps_mod_exc:
            _jps_fail("compressed writer (%s): %s" % (_modname, repr(_jps_mod_exc)))

    try:
        _jps_log("B4: shutil/archive/compressed-file guards done")
    except Exception:
        pass


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
                    if is_write and _is_consent_decision(s):
                        _deny_decision_write("nt.open(write)", s)
                    if not _inside(s, [ROOT] if is_write else READ_ROOTS):
                        _fs_deny("nt.open" + ("(write)" if is_write else ""), s)
                return _orig_nt_open(path, flags, *a, **k)
            _nt.open = _nt_open

            # nt.stat / nt.lstat -- raw metadata; scope like os.stat (READ_ROOTS).
            for _n in ("stat", "lstat"):
                if hasattr(_nt, _n):
                    def _make_ntmeta(nm, orig_fn):
                        def wrapped(path, *a, **k):
                            s = _as_path_str(path)
                            if s is not None and not _inside(s, READ_ROOTS) \
                                    and not _consent_ok(s):
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
                    if (s is not None and not _inside(s, [ROOT]) and not _in_path_dirs(s)
                            and not _consent_ok(s)):
                        return False
                    return _orig_ntacc(path, *a, **k)
                _nt.access = _nt_access

            # RAW ENUMERATION + RAW PATH PREDICATES (2026-11-14, verified leaks). Stage 6
            # patched only nt.open/stat/lstat/access, so the raw enumeration and existence
            # primitives stayed wide open: nt.listdir(C:\Windows\System32) -> 4892 entries,
            # nt.scandir -> 4892, nt._path_exists(win.ini) -> True, nt._path_isdir -> True,
            # nt._getfinalpathname -> '\\\\?\\C:\\Windows\\win.ini'. os.listdir/scandir are
            # covered by the audit hook, but nt.* are called directly by ntpath and by user
            # code holding the module, and the *_path_* / *_pathname predicates fire NO
            # audit event at all -- they leak path EXISTENCE and the resolved real path.
            for _n in ("listdir", "scandir"):
                if hasattr(_nt, _n):
                    def _make_ntenum(nm, orig_fn):
                        def wrapped(path, *a, **k):
                            s = _as_path_str(path)
                            if s is not None and not _inside(s, READ_ROOTS) \
                                    and not _consent_ok(s):
                                _fs_deny("nt." + nm + " (enumeration)", s)
                            return orig_fn(path, *a, **k)
                        wrapped.__name__ = nm
                        return wrapped
                    setattr(_nt, _n, _make_ntenum(_n, getattr(_nt, _n)))

            for _n in ("_path_exists", "_path_isdir", "_path_isfile"):
                if hasattr(_nt, _n):
                    def _make_ntpred(nm, orig_fn):
                        def wrapped(path, *a, **k):
                            s = _as_path_str(path)
                            if (s is not None and not _inside(s, [ROOT]) and not _in_path_dirs(s)
                                    and not _consent_ok(s)):
                                return False  # existence predicate: no leak outside ROOT
                            return orig_fn(path, *a, **k)
                        wrapped.__name__ = nm
                        return wrapped
                    setattr(_nt, _n, _make_ntpred(_n, getattr(_nt, _n)))

            # nt._getfinalpathname / _getfullpathname return the RESOLVED absolute path of
            # an outside file -- pure metadata leak, no audit event. Scope like stat.
            for _n in ("_getfinalpathname", "_getfullpathname"):
                if hasattr(_nt, _n):
                    def _make_ntpathinfo(nm, orig_fn):
                        def wrapped(path, *a, **k):
                            s = _as_path_str(path)
                            if s is not None and not _inside(s, READ_ROOTS) \
                                    and not _consent_ok(s):
                                raise FileNotFoundError(
                                    "[Errno 2] No such file or directory (outside working_root -- "
                                    "python_exec sandbox blocks metadata access outside %s)" % root
                                )
                            return orig_fn(path, *a, **k)
                        wrapped.__name__ = nm
                        return wrapped
                    setattr(_nt, _n, _make_ntpathinfo(_n, getattr(_nt, _n)))

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
                            _fs_deny("_winapi.CopyFile2 (source)", ss)
                        if ds is not None and not _inside(ds, [ROOT]):
                            _fs_deny("_winapi.CopyFile2 (destination)", ds)
                        return _orig_cf2(src, dst, *a, **k)
                    _wapi.CopyFile2 = _cf2

                # CreateFile -- raw handle factory. Write/modify access => ROOT only;
                # read-only handles => READ_ROOTS. Fail-closed: if the access value
                # cannot be determined (unusual arg shape), treat it as a write.
                if hasattr(_wapi, "CreateFile"):
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
                            creation = k.get("dwCreationDisposition")
                            if creation is None and len(a) > 3 and isinstance(a[3], int):
                                creation = a[3]
                            if _winapi_is_write(acc, creation):
                                if _is_consent_decision(s):
                                    _deny_decision_write("_winapi.CreateFile(write)", s)
                                if not _inside(s, [ROOT]):
                                    _fs_deny("_winapi.CreateFile(write)", s)
                            elif not _inside(s, READ_ROOTS):
                                _fs_deny("_winapi.CreateFile(read)", s)
                        return _orig_cfile(name, *a, **k)
                    _wapi.CreateFile = _cfile

                # CreateProcess -- the raw spawn primitive (2026-11-14, verified leak).
                # Unlike the AUDIT event, the wrapper sees the real command_line, so the
                # program basename AND every absolute-path argument are checked here. The
                # shell-OPERATOR rule is not applied to command_line: subprocess builds it
                # with list2cmdline(), so Windows paths contain '\\' and the operator rule
                # would break every legitimate subprocess.run. CPython's own shell wrapper
                # (os.popen / shell=True / os.system -> '<COMSPEC> /c "<cmd>"') is recognised
                # so those routes keep working -- their inner command was already policed by
                # the patched os.system/os.popen/Popen.__init__ path.
                if hasattr(_wapi, "CreateProcess"):
                    _orig_cproc = _wapi.CreateProcess
                    def _cproc(exe=None, cmd=None, *a, **k):
                        # cwd= positional slot 7 of _winapi.CreateProcess (executable, args,
                        # fileattr, pipeattr, inheritable, creationflags, env, cwd,
                        # startupinfo) or the keyword. The AUDIT branch above already checks
                        # args[2] for every route; this wrapper is the second net for calls
                        # that reach the ORIGINAL function extracted from this closure --
                        # those still fire the audit event, but the wrapper also lets us
                        # report the caller's exact cwd value.
                        _check_cwd(k.get("cwd") if "cwd" in k else (a[7] if len(a) > 7 else None))
                        es = _as_path_str(exe)
                        if es is not None:
                            if not _inside(es, READ_ROOTS) and not _in_path_dirs(es):
                                _fs_deny("_winapi.CreateProcess (executable)", es)
                            _check_prog_base(_jps_basename(es), exe)
                        cs = cmd if isinstance(cmd, str) else (
                            cmd.decode("utf-8", "replace") if isinstance(cmd, (bytes, bytearray)) else None)
                        if cs is not None:
                            toks = _jps_tokens(cs)
                            if toks:
                                b0 = _jps_basename(toks[0])
                                wrapped = _is_denied_prog(b0) and len(toks) > 2 and toks[1] in ("/c", "/C")
                                if not wrapped:
                                    _check_prog_base(b0, cs)
                            _check_cmd_paths(toks, cs)
                        return _orig_cproc(exe, cmd, *a, **k)
                    _wapi.CreateProcess = _cproc
            except Exception as _jps_mod_exc:
                _jps_fail("raw win32 (_winapi): %s" % repr(_jps_mod_exc))
    except Exception as _jps_stage_exc:
        _jps_fail("stage 6 (raw win32): %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B5: raw Win32 stage done (%s)" % ("patched" if _IS_WIN else "skipped, not Windows"))
    except Exception:
        pass


    # builtins.open -- defense-in-depth on top of the audit hook (catches builds or
    # contexts where addaudithook is unavailable/removed by user code before an open).
    try:
        import builtins as _jps_builtins
        _orig_builtin_open = _jps_builtins.open
        _MISSING = object()

        def _builtin_open(file, mode="r", *a, **k):
            is_write = any(ch in str(mode) for ch in "wax+")
            # Path-like objects (pathlib.Path) are NOT str/bytes: the audit hook
            # already validates them, but the LINE-ENDING POLICY below must still
            # apply -- pathlib.Path.write_text() goes through this function.
            if isinstance(file, (str, bytes)):
                s = file.decode("utf-8", "replace") if isinstance(file, bytes) else file
                if _is_devnull(s) or _is_dev_or_pipe(s):
                    return _orig_builtin_open(file, mode, *a, **k)  # device name (nul /dev/null)
                if is_write and _is_consent_decision(s):
                    _deny_decision_write("open(write)", s)
                if not _inside(s, [ROOT] if is_write else READ_ROOTS):
                    _fs_deny("open" + ("(write)" if is_write else ""), s)
            # LINE-ENDING POLICY (2026-10-23): text-mode writes must NOT let the OS
            # translate "\n" into os.linesep. On Windows the old behavior produced:
            #   * CRLF in every file written with open(p, 'w'), and
            #   * "\r\r\n" (double CR) whenever the content already contained CRLF,
            # because the translation turned the LF of an existing CRLF into CRLF.
            # A read-modify-write also converted whole LF files to CRLF. Passing
            # newline="\n" disables translation entirely ("\n" and "" mean no
            # translation on write), so bytes written are the bytes the caller built.
            # Explicit newline=""/"\r\n" from the caller is always honored.
            if is_write and "b" not in str(mode):
                if len(a) >= 4:
                    if a[3] is None:
                        a = a[:3] + ("\n",) + a[4:]
                elif k.get("newline", _MISSING) is _MISSING or k.get("newline") is None:
                    k["newline"] = "\n"
            return _orig_builtin_open(file, mode, *a, **k)

        _jps_builtins.open = _builtin_open

        # io.open is the SAME function object in CPython, but rebinding builtins.open
        # does not update the io module attribute. pathlib.Path.write_text()/open()
        # call io.open and explicitly pass newline=None, so patching io.open (and
        # treating newline=None as "unspecified") is required to enforce the policy
        # for pathlib and for libraries that import io.open directly.
        try:
            import io as _jps_io
            if getattr(_jps_io, "open", None) is _orig_builtin_open:
                _jps_io.open = _builtin_open
        except Exception:
            pass
    except Exception as _jps_stage_exc:
        _jps_fail("builtins.open patch: %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B6: builtins.open containment installed")
    except Exception:
        pass


    # socket.connect / create_connection -- outbound policy: LOOPBACK ONLY by default
    # (2026-09-15); ADMIN OPEN NETWORK via JACK_PYEXEC_NETWORK=1 (2026-09-30).
    # The loopback-only rule was added after verified exfiltration probes and stays the
    # fail-closed DEFAULT. Admins who need outbound network from inside python_exec
    # (e.g. HuggingFace API checks, small file fetches) opt in per-run by setting
    # JACK_PYEXEC_NETWORK=1 in the environment of the process that CALLS python_exec --
    # the parent forwards it to this child and this stage then installs NO socket
    # restrictions at all (full admin: every process available). Without the env var,
    # behavior is byte-for-byte identical to before.
    _NET_OPEN = False  # default policy; flipped inside the stage when the admin env is set
    # ---- OUT-OF-SANDBOX NETWORK CONSENT GATE (2026-11-24) ------------------------
    # A blocked non-loopback socket target no longer dies instantly: the child writes a
    # request file to <working_root>/.temp/.jack_netreq/<id>.json and waits (polling,
    # up to JACK_PYEXEC_NET_ASK_TIMEOUT seconds, default 120) for a matching
    # <id>.decision.json written by the PARENT. The parent (python_exec.py) forwards
    # every request to the UI as an approval card ("Allow this connection" / "Deny");
    # its answer is what lands in the decision file. No responder / no answer in time
    # -> DENY (fail-closed, identical outcome to the old hard block). Decisions are
    # cached per target for the rest of THIS run only; concurrent asks for the same
    # target wait on ONE request. The loopback fast path and sendfile's blanket deny
    # never ask -- exactly as before.
    _NET_ASK_TIMEOUT = 120.0
    try:
        _v = float((_jps_os.environ.get("JACK_PYEXEC_NET_ASK_TIMEOUT") or "").strip() or "120")
        if _v > 0:
            _NET_ASK_TIMEOUT = min(_v, 600.0)
    except Exception:
        pass

    # Consent relay switch: the PARENT sets JACK_PYEXEC_NET_CONSENT=1 only when it can
    # actually deliver requests to a UI approval card (live chat channel + websocket).
    # Without a responder we keep the ORIGINAL instant hard block -- no waiting, no files.
    _NET_CONSENT_ON = (_jps_os.environ.get("JACK_PYEXEC_NET_CONSENT", "0") or "").strip() in ("1", "true", "yes")

    import json as _jps_json
    import time as _jps_time
    _NET_REQ_DIR = None
    try:
        _NET_REQ_DIR = _jps_os.path.join(root, ".temp", ".jack_netreq")
        _jps_os.makedirs(_NET_REQ_DIR, exist_ok=True)
    except Exception:
        _NET_REQ_DIR = None

    import threading as _jps_th2
    _NET_LOCK = _jps_th2.Lock()
    _NET_DECIDED = {}   # (host, port) -> "allow" | "deny"  (this run only)
    _NET_INFLIGHT = {}  # (host, port) -> request_id       (dedupe concurrent asks)

    def _net_target_host_port(address):
        if not isinstance(address, (tuple, list)) or not address:
            return None
        host = str(address[0]).strip("[]").lower()
        try:
            port = int(address[1]) if len(address) > 1 else 0
        except Exception:
            port = 0
        return (host, port)

    def _net_ask(host, port, method):
        """Ask the local user for this out-of-sandbox connection. Returns True=allow."""
        key = (host, port)
        with _NET_LOCK:
            d = _NET_DECIDED.get(key)
            if d is not None:
                return d == "allow"
            rid = _NET_INFLIGHT.get(key)
            if rid is None:
                if _NET_REQ_DIR is None:
                    return False  # cannot even write the request -> fail closed
                import uuid as _jps_uuid
                rid = _jps_uuid.uuid4().hex[:12]
                req_path = _jps_os.path.join(_NET_REQ_DIR, rid + ".json")
                try:
                    with open(req_path, "w", encoding="utf-8") as _rf:
                        _jps_json.dump({"request_id": rid, "host": host, "port": port,
                                        "method": method}, _rf)
                except Exception:
                    return False  # fail closed -- no request, no connection
                _NET_INFLIGHT[key] = rid

        deadline = _jps_time.monotonic() + _NET_ASK_TIMEOUT
        while True:
            try:
                with open(_jps_os.path.join(_NET_REQ_DIR, rid + ".decision.json"),
                          "r", encoding="utf-8") as _df:
                    dec = (_jps_json.load(_df) or {}).get("decision")
            except Exception:
                dec = None
            if dec in ("allow", "deny"):
                with _NET_LOCK:
                    _NET_DECIDED[key] = dec
                    _NET_INFLIGHT.pop(key, None)
                return dec == "allow"
            if _jps_time.monotonic() >= deadline:
                break  # no answer in time -> deny (fail-closed)
            try:
                _jps_th2.Event().wait(0.15)
            except Exception:
                _jps_time.sleep(0.15)
        with _NET_LOCK:
            _NET_DECIDED[key] = "deny"
            _NET_INFLIGHT.pop(key, None)
        return False

    def _net_gate(address, method):
        """Raise PermissionError for a non-loopback target unless the user allows it."""
        if not isinstance(address, (tuple, list)):
            return  # let CPython raise its own TypeError
        host = str(address[0]).strip("[]").lower() if address else ""
        h = host
        if not (h == "localhost" or h.startswith("127.") or h == "::1"):
            if not _NET_CONSENT_ON:
                raise PermissionError(
                    "python_exec sandbox: socket %s to %r is blocked -- only loopback (localhost) "
                    "connections are allowed inside python_exec. "
                    "(admin: set JACK_PYEXEC_NETWORK=1 in the calling process env)"
                    % (method, address))
            tp = _net_target_host_port(address)
            if tp is None:
                raise PermissionError(
                    "python_exec sandbox: socket %s to %r is blocked -- only loopback "
                    "(localhost) connections are allowed inside python_exec." % (method, address))
            ok = _net_ask(tp[0], tp[1], method)
            if not ok:
                raise PermissionError(
                    "python_exec sandbox: socket %s to %r was NOT allowed -- the local user "
                    "denied it or did not answer in time. Only loopback (localhost) "
                    "connections are allowed inside python_exec." % (method, address))

    # ---- OUT-OF-SANDBOX FILE CONSENT GATE (2026-11-24) --------------------------
    # A blocked outside-working_root file operation no longer dies instantly when a UI
    # relay is live: the child writes a request file to <working_root>/.temp/.jack_netreq/
    # <id>.json with kind="file" and waits (up to JACK_PYEXEC_NET_ASK_TIMEOUT seconds,
    # same budget as the network gate) for the parent's decision file. The parent
    # relays it to the chat UI as an approval card ("Allow this file access" / "Deny").
    # Without a relay (JACK_PYEXEC_NET_CONSENT=0) behavior is byte-for-byte identical
    # to the original instant hard block -- same message, no waiting, no files.
    # Decisions are cached per resolved path for THIS run only; an allow/deny on a
    # directory covers its whole subtree (prevents dialog spam when os.walk / shutil
    # touch many children of one allowed area). Fail-closed everywhere: timeout or any
    # internal error denies. Non-file denials (imports, zip member names, exec policy)
    # keep using the plain _deny() hard block -- they are never askable.
    _FS_DECIDED = {}   # normcase'd resolved path -> "allow" | "deny"  (this run only)
    _FS_INFLIGHT = {}  # normcase'd resolved path -> request_id       (dedupe concurrent asks)
    def _fs_cached(norm_p):
        """Ancestor-aware cache lookup: a decision on a parent directory covers its subtree."""
        for k, d in _FS_DECIDED.items():
            if norm_p == k or norm_p.startswith(k + _jps_os.sep):
                return d
        return None
    def _fs_deny(what, p):
        """File denial with the consent option: instant hard block unless a UI relay is
        live, in which case ASK THE LOCAL USER (fail-closed on timeout/error)."""
        if not _NET_CONSENT_ON or _NET_REQ_DIR is None:
            _deny(what, p)  # original message, byte-for-byte identical -- no waiting
        try:
            np = _norm(p)
        except Exception:
            _deny(what, p)  # cannot resolve the path -> fail closed with the original block
        with _NET_LOCK:
            d = _fs_cached(np)
            if d is not None:
                rid = None  # already decided (this run) -- no ask needed
            else:
                rid = _FS_INFLIGHT.get(np)
                if rid is None:
                    import uuid as _jps_uuid
                    rid = _jps_uuid.uuid4().hex[:12]
                    req_path = _jps_os.path.join(_NET_REQ_DIR, rid + ".json")
                    try:
                        with open(req_path, "w", encoding="utf-8") as _rf:
                            _jps_json.dump({"request_id": rid, "kind": "file",
                                            "op": what, "path": p}, _rf)
                    except Exception:
                        _deny(what, p)  # cannot even write the request -> fail closed
                    _FS_INFLIGHT[np] = rid
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
                _FS_DECIDED[np] = dec if dec in ("allow", "deny") else "deny"
                _FS_INFLIGHT.pop(np, None)
            d = _FS_DECIDED[np]
        if d == "allow":
            return  # the user allowed this path (and its subtree) -- let it proceed
        raise PermissionError(
            "python_exec sandbox: %s of '%s' was NOT allowed -- it is outside working_root (%s) "
            "and the local user denied it or did not answer in time." % (what, p, root))

    try:
        import socket as _jps_sock

        if (_jps_os.environ.get("JACK_PYEXEC_NETWORK", "0") or "").strip() in ("1", "true", "TRUE", "yes"):
            _NET_OPEN = True  # admin open network -- no socket restrictions installed below

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
        if not _NET_OPEN:
            _sock_orig_connect = _jps_sock.socket.connect
            def _sock_connect(self, address):
                _net_gate(address, "connect")
                return _sock_orig_connect(self, address)
            _jps_sock.socket.connect = _sock_connect

            # NOTE (audit 2026-09-16): socket.connect is a C method and connect_ex /
            # sendto / sendall are SEPARATE methods -- the .connect-only patch left an
            # unconnected-UDP sendto and a connect_ex fully open (verified probes V1/V2).
            if hasattr(_jps_sock.socket, "connect_ex"):
                _sock_orig_cex = _jps_sock.socket.connect_ex
                def _sock_connect_ex(self, address):
                    _net_gate(address, "connect_ex")
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
                            if addr is not None:
                                _net_gate(addr, nm)
                            return orig_fn(self, *a, **k)
                        guarded.__name__ = nm
                        return guarded
                    setattr(_jps_sock.socket, _m, _make_send_guard(_orig_m, _m))

            if hasattr(_jps_sock.socket, "sendmsg"):
                _sock_orig_sm = _jps_sock.socket.sendmsg
                def _sock_sendmsg(self, *a, **k):
                    addr = k.get("addr", a[1] if len(a) > 1 else None)
                    if isinstance(addr, tuple):
                        _net_gate(addr, "sendmsg")
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
                    _net_gate(address, "create_connection")
                    return _sock_orig_cc(address, *a, **k)
                _jps_sock.create_connection = _sock_cc
    except Exception as _jps_stage_exc:
        _jps_fail("socket patch: %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B7: socket policy installed (%s)" % ("OPEN NETWORK (admin JACK_PYEXEC_NETWORK=1)" if _NET_OPEN else "loopback-only"))
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
        _jps_fail("importlib loader patch: %s" % repr(_jps_stage_exc))

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
                    _fs_deny("sqlite3.connect (database path)", s)
            return _orig_connect(database, *a, **k)
        _sqlite3.connect = _connect
    except Exception as _jps_stage_exc:
        _jps_fail("sqlite3 patch: %s" % repr(_jps_stage_exc))

    try:
        _jps_log("B8: importlib/sqlite3 guards done")
    except Exception:
        pass



_jps_setup()
try:
    import sys as _jps_sys3, os as _jps_os3
    if _jps_os3.environ.get("JACK_PYEXEC_DEBUG", "0") != "0":
        _ld = _jps_os3.path.join(_jps_os3.environ.get("JACK_PYEXEC_ROOT") or ".", ".temp")  # user policy (2026-09-18): child debug log lives in .temp/
        _jps_os3.makedirs(_ld, exist_ok=True)
        with open(_jps_os3.path.join(_ld, ".jack_pyexec_child.log"), "a", encoding="utf-8") as _lf3:
            _lf3.write("[PYEXEC] B9: sandbox bootstrap complete -- user code may now run" + chr(10))
except Exception:
    pass
# === end python_exec runtime sandbox ===
'''  # embedded literal (2026-09-23): see DELIVERY NOTE above


# ---------------------------------------------------------------------------
# PROVENANCE FRAGMENTS -- _sandbox_parts/ is DATA, not a runtime dependency.
# ---------------------------------------------------------------------------
# The split fragments in tools/exec_tools/_sandbox_parts/ exist ONLY as an auditable
# provenance view of the literal above (one file per original stage). They are NOT read
# at import time: the CLIENT execs this module's source in-memory with no __file__ and a
# cwd that is the user's working_root, where no repo folder exists -- any disk read here
# would kill python_exec on every client (the 2026-09-18 split bug, fixed 2026-09-23).
# Regenerate them byte-for-byte with:  python tools/exec_tools/_regen_sandbox_parts.py
# The test suite enforces parts == literal so they can never drift silently.

_SANDBOX_PART_FILES = [
    "_part_00_header.py",            # literal L2-5     banner + stdlib aliases
    "_part_01_core.py",              # literal L6-304   root/READ_ROOTS/_norm/_inside/_deny/logging; _jps_setup def starts here
    "_part_02_exec_policy_data.py",  # literal L305-622 stage 0: exec-policy denylists + tokenizers + _check_cmd + _check_cwd + consent gates + spawn token
    "_part_03_audit_hook.py",        # literal L623-897 stage 1: sys.addaudithook enforcement (incl. raw-Win32/socket branches, cwd checks, fail-closed)
    "_part_04_os_patches.py",        # literal L898-1190 stage 2: os patches + metadata scoping + flag-aware os.open
    "_part_05_subprocess_policy.py", # literal L1191-1342 stage 3: Popen/spawn/exec/fork policy (incl. cwd= in Popen.__init__)
    "_part_06_shutil.py",            # literal L1343-1455 stage 4: shutil high-level guards
    "_part_07_archives.py",          # literal L1456-1604 stage 5: zip/tar/gzip/bz2/lzma writers
    "_part_08_win32_misc.py",        # literal L1605-2274 stage 6: raw Win32 + builtins.open + sockets + importlib/sqlite3; last line ends _jps_setup()
    "_part_09_footer.py",            # literal L2275-2286 module-level tail: _jps_setup() call + B9 log + end marker
]

_FRAGMENT_MARKER = "# === FRAGMENT START (verbatim) ==="

# One layout anchor per part file -- the minimal leading line prefix of each original
# fragment that is unique in the whole literal. Fixed anchors by design: if the stage
# layout ever changes, split_into_fragments() raises loudly instead of writing corrupt
# provenance files.
_PART_BANNER_LINES = [
    '# === JACK python_exec runtime sandbox (auto-prepended; do not edit) ===',
    'def _jps_setup():',
    '    # ---- 0. CHILD-PROCESS EXEC POLICY data + helpers (2026-09-15 Mac fix) -----',
    '    # ---- 1. AUDIT HOOK: primary runtime enforcement -------------------------',
    '    # ---- 2. PATCH primitives not fully covered by an audited event ----------',
    '    # ---- 3. CHILD-PROCESS EXEC POLICY patches (second net over the hook) --',
    '    # ---- 4. shutil HIGH-LEVEL FILE FUNCTIONS (hardening 2026-09-17) -------',
    '    # ---- 5. ARCHIVE + COMPRESSED-FILE WRITERS (hardening 2026-09-17) ------',
    '    # ---- 6. RAW WIN32 FILE PRIMITIVES (Windows; hardening 2026-09-17) -----',
    '_jps_setup()\ntry:',
]


def split_into_fragments(value=None):
    """Split the embedded literal back into its provenance fragments.

    Returns a list of (filename, fragment_text) tuples in _SANDBOX_PART_FILES order;
    the join contract is value == chr(10) + "".join(fragments). Used by the regeneration
    script and by the test that pins _sandbox_parts/ to this literal byte-for-byte.
    Raises ValueError when a layout anchor is missing (literal layout changed) -- never
    returns partial fragments.
    """
    if value is None:
        value = SANDBOX_BOOTSTRAP
    if not value.startswith(chr(10)):
        raise ValueError("unexpected literal head -- join contract changed")
    body = value[1:]
    starts = []
    pos = 0
    if len(_SANDBOX_PART_FILES) != len(_PART_BANNER_LINES):
        raise ValueError("part-file list and anchor list drifted (%d vs %d)"
                         % (len(_SANDBOX_PART_FILES), len(_PART_BANNER_LINES)))
    for fname, anchor in zip(_SANDBOX_PART_FILES, _PART_BANNER_LINES, strict=True):
        idx = body.find(anchor, pos)
        if idx == -1:
            raise ValueError("layout anchor not found for %s -- literal layout changed" % fname)
        starts.append(idx)
        pos = idx + 1
    out = []
    for i, fname in enumerate(_SANDBOX_PART_FILES):
        end = starts[i + 1] if i + 1 < len(starts) else len(body)
        out.append((fname, body[starts[i]:end]))
    return out


# HARD-CRASH CONTRACT (2026-09-18, preserved): the literal above is validated at import
# time -- any syntax error in it raises immediately so a broken sandbox gets fixed instead
# of silently degrading. Do NOT wrap this in try/except: shipping an untested or stale
# containment layer into every python_exec child would be worse than the crash it hides.
compile(SANDBOX_BOOTSTRAP, "<SANDBOX_BOOTSTRAP>", "exec")  # fail closed on any syntax error
