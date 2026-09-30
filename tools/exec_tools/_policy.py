"""python_exec security policy -- pure data, zero logic, zero dependencies.

This module is DELIBERATELY DATA-ONLY so it can be shipped to the CLIENT sandbox
without any import machinery (the CLIENT's dynamic_loader rewrites same-package
imports into bare-name assignments; see _policy loading in python_exec.py).

WHAT LIVES HERE
---------------
  * FORBIDDEN_MODULES           -- default per-profile import blocklist data
                                   (config/blocked_libs_set01.json must equal this)
  * DANGEROUS_MODULES_FROM_IMPORT / SAFE_OS_IMPORTS -- os/importlib from-import policy
  * DEFAULT_FORBIDDEN_MODULES   -- fail-safe set used when no profile set is available
  * FORBIDDEN_FUNCTIONS         -- builtins that can never be CALLED (directly or via alias)
  * HARD_FLOOR_ATTR_CALL_NAMES  -- builtin names blocked as attribute calls on ANY object
                                   (the hard floor added by the 2026-07-14 refactor:
                                    closes the globals()['__builtins__'].open / .exec route)
  * DANGEROUS_ATTR_ACCESS       -- dunder attributes blocked at ACCESS time
  * TINTED_CALL_NAMES           -- calls whose RESULT is always dangerous (globals/vars/...)
  * HIGHER_ORDER_FUNCTIONS      -- functions checked for forbidden-function arguments
  * FORBIDDEN_ATTR_CALLS        -- module.function pairs that are never allowed
  * FORBIDDEN_PATTERNS          -- regex defense-in-depth rules (run on string-masked code)

WHY A HARD FLOOR EXISTS (2026-07-14 security audit, verified live exploit)
--------------------------------------------------------------------------
The per-profile blocked-libs set only governs IMPORT statements. An empty admin
set means "no restrictions at all" BY DESIGN -- that is the user requirement and
must be preserved. For every NON-empty profile (e.g. blocked_libs_set01.json) a
hard floor of checks runs REGARDLESS of what the profile set contains, because
the AST/regex guardrails over arbitrary Python are fundamentally whack-a-mole:

    b = globals()['__builtins__']   # in `python -c` context this is the BUILTINS MODULE
    f = b.open                      # no forbidden token anywhere in source
    print(f(r'<SYSTEM_DIR>\\sensitive.txt').read())   # example path -- reads OUTSIDE working_root (proven, rc=0)

The hard floor blocks: attribute access to open/exec/eval/compile/__import__ on
ANY object, subscript chains rooted at globals()/vars() calls, and getattr with a
dynamic name. The AST visitor in _ast_guard.py implements it; the regex rules
here are the second layer.
"""


# ---------------------------------------------------------------------------
# Forbidden Modules -- cannot be imported at all (DEFAULT profile data).
# FIX (2026-08-19): "glob" re-forbidden -- live test proved glob.glob() can
#      enumerate ANY directory on disk, i.e. outside working_root. pathlib stays
#      forbidden for the same reason; posixpath/ntpath are the os.path backends
#      and are blocked to close aliasing routes.
# FIX (2026-08-19): archive modules take an arbitrary path string and can read/
#      extract files ANYWHERE on disk -- same escape class as glob/pathlib.
# FIX (2026-07-15): pathlib fully forbidden, not just a few methods.
# ---------------------------------------------------------------------------
FORBIDDEN_MODULES = frozenset({
    # File system modules (glob re-forbidden 2026-08-19 -- see live escape test)
    "shutil", "tempfile", "stat", "fileinput",
    "glob", "posixpath", "ntpath",
    # Archive modules (2026-08-19): read/extract from ANY path on disk.
    "zipfile", "tarfile",
    # Subprocess / shell
    "subprocess", "popen2", "commands", "pty",
    # Network
    "socket", "http.client", "urllib.request", "httplib",
    # Serialization (can execute arbitrary code)
    "pickle", "shelve", "marshal", "copyreg",
    # Native code
    "ctypes", "cffi",
    # Pathlib (2026-07-15): read_text/read_bytes/open/iterdir/glob all escape.
    "pathlib",
    # Database
    "sqlite3", "mysql.connector", "psycopg2",
    # System / process control
    "resource", "grp", "pwd", "fcntl", "termios",
    # sys.modules is a live registry of every loaded module -- looking up 'os'
    # there bypasses all import checks (live escape test 2026-08-19).
    "sys", "builtins",
    # File readers that take an arbitrary path string (live escape tests).
    "io", "codecs", "inspect",
    # Script runners / file readers that take an arbitrary path.
    "runpy", "linecache", "pkgutil", "tokenize",
})


# ---------------------------------------------------------------------------
# Dangerous Modules -- `from X import Y` is blocked per-name (see SAFE_OS_IMPORTS)
# ---------------------------------------------------------------------------
DANGEROUS_MODULES_FROM_IMPORT = frozenset({"os", "importlib"})

# ---------------------------------------------------------------------------
# DEFAULT blocked-libs set -- used when NO per-profile set is available.
# Equals FORBIDDEN_MODULES plus the from-import-governed modules, so legacy
# behavior stays as a fail-safe: when in doubt we restrict, never allow all.
# (defined AFTER DANGEROUS_MODULES_FROM_IMPORT so the name exists at import time)
# ---------------------------------------------------------------------------
DEFAULT_FORBIDDEN_MODULES = frozenset(FORBIDDEN_MODULES) | DANGEROUS_MODULES_FROM_IMPORT

SAFE_OS_IMPORTS = frozenset({
    # Constants + pure path math only. 2026-08-19: "path", existence checks,
    # realpath and abspath were REMOVED -- they probe ANY path on disk.
    "getcwd", "curdir", "pardir", "sep", "altsep", "pathsep",
    "name", "linesep", "devnull",
    # Pure path math only -- string operations, no disk access.
    "join", "basename", "dirname", "splitext", "split", "normpath",
    "relpath", "commonprefix", "isabs", "expanduser", "expandvars",
    # Environment read-only access (os.environ itself, not mutation)
    "environ",
    # OS constants (read-only values: R_OK, W_OK, X_OK, F_OK, etc.)
    "R_OK", "W_OK", "X_OK", "F_OK",
})

# ---------------------------------------------------------------------------
# Forbidden Functions -- builtins that can never be CALLED.
# Checked as direct Name calls AND as aliases (x = eval -> x() blocked).
# ---------------------------------------------------------------------------
FORBIDDEN_FUNCTIONS = frozenset({
    # Dynamic code execution
    "eval", "exec", "compile", "__import__",
    # File I/O
    "open",
    # Process control
    "exit", "_exit",
})

# ---------------------------------------------------------------------------
# HARD FLOOR (2026-07-14): builtin names blocked as ATTRIBUTE CALLS on ANY object.
# This is the fix for the verified globals()['__builtins__'] bypass: in a
# `python -c` context globals()['__builtins__'] IS the builtins module, so
# `b = globals()['__builtins__']; f = b.open; f(...)` pulled open() off it with
# zero forbidden tokens in source. These names are blocked wherever they appear
# as an attribute call, on any base object, for every non-empty profile set.
# (Direct Name calls to the same names are already covered by FORBIDDEN_FUNCTIONS.)
# ---------------------------------------------------------------------------
HARD_FLOOR_ATTR_CALL_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__",
})

# ---------------------------------------------------------------------------
# Dangerous Attribute Access -- dunder attributes blocked at ACCESS time (not
# only when called). 2026-08-19: __dict__ added after the live escape test where
# os.__dict__['system'] pulled the shell function off a module.
# ---------------------------------------------------------------------------
DANGEROUS_ATTR_ACCESS = frozenset({
    "__builtins__", "__import__", "__subclasses__", "__mro__",
    "__globals__", "__code__", "__class__.__bases__",
    "func_globals", "func_code", "co_code",
    "__dict__",
})

# ---------------------------------------------------------------------------
# Tinted call names -- calling one of these taints its RESULT: any attribute
# access or subscript on the result is dangerous. globals()/vars() return dicts
# that in script context contain '__builtins__' (the builtins MODULE); a bare
# __builtins__ reference resolves to that module too.
# ---------------------------------------------------------------------------
TINTED_CALL_NAMES = frozenset({"globals", "vars", "__builtins__"})

# ---------------------------------------------------------------------------
# Higher-Order Functions -- checked for forbidden function arguments
# ---------------------------------------------------------------------------
HIGHER_ORDER_FUNCTIONS = frozenset({
    "map", "filter", "reduce", "sorted", "min", "max",
    "any", "all", "list", "tuple", "set",
})

# ---------------------------------------------------------------------------
# Forbidden Attribute Calls -- module.function pairs that are never allowed.
# (Read-only os inspection methods were removed 2026-07-15; file-mutating and
# process-control methods remain blocked.)
# ---------------------------------------------------------------------------
FORBIDDEN_ATTR_CALLS = frozenset({
    # Subprocess (can run ANY shell command)
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_output"), ("subprocess", "check_call"),

    # OS-level process control & file manipulation
    ("os", "system"), ("os", "popen"), ("os", "execvp"), ("os", "execlp"),

    # FileSystem inspection (2026-07-15) -- can enumerate/read metadata of ANY path.
    ("os", "walk"), ("os", "listdir"), ("os", "scandir"), ("os", "readlink"),
    ("os", "stat"), ("os", "lstat"), ("os", "access"),
    ("os", "spawnl"), ("os", "spawnle"), ("os", "spawnv"), ("os", "spawnve"),
    ("os", "fork"), ("os", "posix_spawn"),

    # File mutation -- DELETE / RENAME / MOVE (blocked)
    ("os", "remove"), ("os", "unlink"), ("os", "rename"), ("os", "replace"),
    ("os", "rmdir"), ("os", "removedirs"),

    # Directory creation outside working_root (blocked)
    ("os", "makedirs"), ("os", "mkdir"),

    # File permission / ownership changes (blocked)
    ("os", "chmod"), ("os", "chown"), ("os", "utime"), ("os", "truncate"),

    # Low-level file descriptor operations (blocked)
    ("os", "open"), ("os", "close"), ("os", "read"), ("os", "write"),

    # Hard links / symlinks (blocked -- can bypass path restrictions)
    ("os", "link"), ("os", "symlink"),

    # Dynamic import
    ("importlib", "import_module"), ("importlib", "util"),

    # Serialization / Persistence (can read/write arbitrary data)
    ("pickle", "loads"), ("pickle", "load"), ("pickle", "dumps"), ("pickle", "dump"),
    ("marshal", "loads"), ("marshal", "load"), ("marshal", "dumps"), ("marshal", "dump"),

    # Network access (can exfiltrate or fetch data)
    ("socket", "socket"), ("http.client", "HTTPConnection"),
    ("urllib.request", "urlopen"),

    # Temp file creation (can write outside working_root)
    ("tempfile", "mktemp"), ("tempfile", "mkstemp"),
    ("tempfile", "NamedTemporaryFile"), ("tempfile", "TemporaryFile"),
    ("tempfile", "mkdtemp"),

    # Native code execution (extreme danger)
    ("ctypes", "CDLL"), ("ctypes", "windll"), ("ctypes", "WinDLL"),

    # Database / shelf access
    ("shelve", "open"), ("sqlite3", "connect"),
})

# ---------------------------------------------------------------------------
# Forbidden Regex Patterns -- defense-in-depth fallback.
# IMPORTANT: these run on STRING-MASKED source (see _pattern_scan.mask_strings),
# so string literals can neither cause false positives nor hide payloads via
# concatenation tricks that the AST layer already handles.
# FIX (2026-07-14): globals()/vars() lookups added -- the root of the verified
# __builtins__ escape route; any subscript through them is a dynamic lookup.
# ---------------------------------------------------------------------------
FORBIDDEN_PATTERNS = {
    # --- File I/O (read/write outside working_root) ---
    r'\bopen\s*\(': 'built-in open() -- cannot use file I/O',
    r'\bos\.open\b': 'os.open() -- cannot use OS-level file operations',
    r'\bos\.close\b': 'os.close() -- cannot use OS-level file operations',
    r'\bos\.read\b': 'os.read() -- cannot read from file descriptors',
    r'\bos\.write\b': 'os.write() -- cannot write to file descriptors',

    # --- File manipulation (delete, rename, move, etc.) ---
    r'\bos\.remove\b': 'os.remove() -- cannot delete files',
    r'\bos\.unlink\b': 'os.unlink() -- cannot unlink files',
    r'\bos\.rename\b': 'os.rename() -- cannot rename/move files',
    r'\bos\.replace\b': 'os.replace() -- cannot replace files',
    r'\bos\.rmdir\b': 'os.rmdir() -- cannot remove directories',
    r'\bos\.removedirs\b': 'os.removedirs() -- cannot remove directory trees',
    r'\bos\.link\b': 'os.link() -- cannot create hard links',
    r'\bos\.symlink\b': 'os.symlink() -- cannot create symbolic links',
    r'\bos\.chmod\b': 'os.chmod() -- cannot change file permissions',
    r'\bos\.chown\b': 'os.chown() -- cannot change file ownership',
    r'\bos\.utime\b': 'os.utime() -- cannot modify file timestamps',
    r'\bos\.truncate\b': 'os.truncate() -- cannot truncate files',
    r'\bos\.mknod\b': 'os.mknod() -- cannot create special files',
    r'\bos\.mkdir\b': 'os.mkdir() -- cannot create directories outside working_root',
    r'\bos\.makedirs\b': 'os.makedirs() -- cannot create directory trees outside working_root',

    # --- Pathlib (2026-07-15): ALL filesystem access is blocked. ---
    r'\bpathlib\b': 'pathlib module -- cannot access the filesystem outside working_root',
    r'\.read_text\s*\(': '.read_text() -- cannot read files via pathlib (escapes working_root)',
    r'\.read_bytes\s*\(': '.read_bytes() -- cannot read files via pathlib (escapes working_root)',

    # --- Pathlib file-mutating operations ---
    r'\.write_text\s*\(': '.write_text() -- cannot write files via pathlib',
    r'\.write_bytes\s*\(': '.write_bytes() -- cannot write files via pathlib',
    r'\.unlink\b': '.unlink() -- cannot delete files via pathlib',
    r'\.rename\s*\(': '.rename() -- cannot rename files via pathlib',
    r'\.rmdir\s*\(': '.rmdir() -- cannot remove directories via pathlib',
    r'\.touch\s*\(': '.touch() -- cannot touch files via pathlib',
    r'\.mkdir\s*\(': '.mkdir() -- cannot create directories via pathlib',

    # --- Shutil (high-level file operations) ---
    r'\bshutil\.': 'shutil module -- cannot use high-level file operations',

    # --- Archive modules (2026-08-19): read/extract from ANY path on disk. ---
    r'\bzipfile\b': 'zipfile module -- cannot access archives outside working_root',
    r'\btarfile\b': 'tarfile module -- cannot access archives outside working_root',

    # --- Subprocess (can run ANY shell command) ---
    r'\bsubprocess\.run\b': 'subprocess.run() -- cannot execute subprocesses',
    r'\bsubprocess\.Popen\b': 'subprocess.Popen() -- cannot spawn processes',
    r'\bsubprocess\.call\b': 'subprocess.call() -- cannot call external programs',
    r'\bsubprocess\.check_output\b': 'subprocess.check_output() -- cannot capture command output',
    r'\bsubprocess\.check_call\b': 'subprocess.check_call() -- cannot check-call commands',
    r'\bos\.system\b': 'os.system() -- cannot execute shell commands',
    r'\bos\.popen\b': 'os.popen() -- cannot open pipes to shell commands',
    r'\bpopen2\.': 'popen2 module -- cannot use popen2 for subprocess calls',

    # --- Dynamic code execution (can bypass all restrictions) ---
    r'\beval\s*\(': 'eval() -- cannot evaluate arbitrary expressions',
    r'\bexec\s*\(': 'exec() -- cannot execute arbitrary code strings',
    r'\bcompile\s*\(': 'compile() -- cannot compile arbitrary code',
    r'__import__\s*\(': '__import__() -- cannot dynamically import modules',
    r'\bimportlib\.import_module\b': 'importlib.import_module() -- cannot dynamically import',

    # --- Dynamic namespace lookups (2026-07-14): the verified escape root. ---
    # globals()['__builtins__'] is the builtins MODULE in `python -c` context;
    # any subscript through globals()/vars() is a dynamic function/object lookup.
    r'\bglobals\s*\(\s*\)\s*\[': 'globals()[...] -- dynamic namespace lookup is blocked',
    r'\bvars\s*\(\s*\)\s*\[': 'vars()[...] -- dynamic namespace lookup is blocked',

    # --- Serialization / Persistence (can read/write arbitrary data) ---
    r'\bpickle\.loads?\b': 'pickle.loads/load() -- cannot deserialize pickle data',
    r'\bpickle\.dumps?\b': 'pickle.dumps/dump() -- cannot serialize to pickle',
    r'\bshelve\.open\b': 'shelve.open() -- cannot use shelve for persistence',
    r'\bsqlite3\.connect\b': 'sqlite3.connect() -- cannot access SQLite databases',
    r'\bmarshal\.loads?\b': 'marshal.loads/load() -- cannot deserialize marshal data',
    r'\bmarshal\.dumps?\b': 'marshal.dumps/dump() -- cannot serialize to marshal',

    # --- Network access (can exfiltrate or fetch data) ---
    r'\bsocket\.socket\b': 'socket.socket() -- cannot create network sockets',
    r'\burllib\.request\.urlopen\b': 'urllib.request.urlopen() -- cannot make HTTP requests',
    r'\bhttp\.client\.HTTPConnection\b': 'http.client.HTTPConnection -- cannot connect via HTTP',

    # --- Temp file creation (can write outside working_root) ---
    r'\btempfile\.mktemp\b': 'tempfile.mktemp() -- cannot create temp files',
    r'\btempfile\.mkstemp\b': 'tempfile.mkstemp() -- cannot create named temp files',
    r'\btempfile\.NamedTemporaryFile\b': 'tempfile.NamedTemporaryFile -- cannot use NamedTemporaryFile',
    r'\btempfile\.TemporaryFile\b': 'tempfile.TemporaryFile -- cannot use TemporaryFile',
    r'\btempfile\.mkdtemp\b': 'tempfile.mkdtemp() -- cannot create temp directories',

    # --- Native code execution (extreme danger) ---
    r'\bctypes\.CDLL\b': 'ctypes.CDLL -- cannot load native libraries',
    r'\bctypes\.windll\b': 'ctypes.windll -- cannot access Windows DLLs',
    r'\bctypes\.WinDLL\b': 'ctypes.WinDLL -- cannot access Windows DLLs',

    # --- Process control ---
    r'\bos\._exit\b': 'os._exit() -- cannot force-exit the process',
}


    # ---------------------------------------------------------------------------
# Ready-made policy namespace.
# python_exec builds its checks against this single object so the guardrail
# modules (_ast_guard / _pattern_scan) stay pure functions of (code, policy).
# Exposed as one PUBLIC name on purpose: the CLIENT's dynamic_loader only
# injects public symbols from delivered same-package deps into tool globals.
# ---------------------------------------------------------------------------
from types import SimpleNamespace as _SimpleNamespace

POLICY = _SimpleNamespace(
    FORBIDDEN_MODULES=FORBIDDEN_MODULES,
    DANGEROUS_MODULES_FROM_IMPORT=DANGEROUS_MODULES_FROM_IMPORT,
    DEFAULT_FORBIDDEN_MODULES=DEFAULT_FORBIDDEN_MODULES,
    SAFE_OS_IMPORTS=SAFE_OS_IMPORTS,
    FORBIDDEN_FUNCTIONS=FORBIDDEN_FUNCTIONS,
    HARD_FLOOR_ATTR_CALL_NAMES=HARD_FLOOR_ATTR_CALL_NAMES,
    DANGEROUS_ATTR_ACCESS=DANGEROUS_ATTR_ACCESS,
    TINTED_CALL_NAMES=TINTED_CALL_NAMES,
    HIGHER_ORDER_FUNCTIONS=HIGHER_ORDER_FUNCTIONS,
    FORBIDDEN_ATTR_CALLS=FORBIDDEN_ATTR_CALLS,
    FORBIDDEN_PATTERNS=FORBIDDEN_PATTERNS,
)
