"""Per-tool security policy for the remote-tool sandbox.

The AST guard (ast_guard.py) blocks dangerous modules and calls BY DEFAULT.
These allowlists are the explicit, auditable exceptions -- one entry per tool
that legitimately needs a "dangerous" capability. Keep this table small: every
row here is an intentional trust decision, not an accident.
"""

# Tool-specific allowlist for imports that are safe in context.
# Maps tool name -> set of top-level module names the tool may import.
ALLOWED_TOOL_IMPORTS: dict[str, set[str]] = {
    "write_file": {"shutil"},
    "move_file": {"shutil"},
    "browser_screenshot": {"ctypes"},
    "fetch_url": {"socket"},
    # Tools that legitimately use subprocess (safe args only)
    "python_exec": {"subprocess"},
    "git_clone": {"subprocess"},
    "run_tests": {"subprocess", "tempfile"},
    # generate_image: subprocess runs its own venv worker; shutil (2026-09-12) does the
    # one-time move of a pre-upgrade model cache from the default user HF location into
    # <runtime_dir>/hf_cache - it only ever touches this tool's own folders, never user files.
    "generate_image": {"subprocess", "shutil"},
    # connect: BETA auto-start launches a FRESH debug-mode Chrome window via
    # subprocess.Popen with a fixed argument list (dedicated --user-data-dir so it
    # always opens as a separate window and binds the port cleanly). The 2026-09-10
    # rewrite had ALSO allowlisted 'socket' + subprocess.run for an inline CDP-repair
    # block; that repair logic taskkill'd the user's browser (the "closed my UI"
    # incident) and was REMOVED on 2026-09-11. The entry is back to the minimal BETA
    # surface: subprocess only, Popen with fixed args. If a future rewrite needs more,
    # it must be an explicit new row here -- not an accident.
    "connect": {"subprocess"},
}

# Tool-specific allowlist for dangerous function calls.
# Maps tool name -> set of 'module.function' call targets the tool may invoke.
ALLOWED_TOOL_CALLS: dict[str, set[str]] = {
    # python_exec: subprocess.run (legacy path) + subprocess.Popen (2026-09-17 macOS
    # hang fix): the bare run() was replaced by a manual Popen loop so stdin can be fed
    # from a dedicated thread, stdout/stderr are drained continuously (no pipe-fill
    # deadlock), a 10s heartbeat is logged while the child runs, and on timeout the
    # WHOLE process group is killed (POSIX start_new_session + killpg). The runtime
    # sandbox in _sandbox_bootstrap.py still restricts WHAT may be spawned (shells,
    # interpreters and network tools are denied at exec time) -- this row only unblocks
    # the static source-validation layer that was rejecting the tool's own code at load.
    "python_exec": {"subprocess.run", "subprocess.Popen"},
    "git_clone": {"subprocess.run"},
    "run_tests": {"subprocess.run"},
    "generate_image": {"subprocess.run"},
    # connect: Popen launches chrome.exe with the debug port (BETA launch logic).
    # NO subprocess.run -- process inspection/kill was part of the removed 2026-09-10
    # repair path and must not come back without a deliberate policy change.
    "connect": {"subprocess.Popen"},
}


def allowed_imports_for(tool_name: str | None) -> set[str]:
    """Return the import allowlist for *tool_name* (empty set = nothing extra allowed)."""
    if tool_name and tool_name in ALLOWED_TOOL_IMPORTS:
        return ALLOWED_TOOL_IMPORTS[tool_name]
    return set()


def allowed_calls_for(tool_name: str | None) -> set[str]:
    """Return the call allowlist for *tool_name* (empty set = nothing extra allowed)."""
    if tool_name and tool_name in ALLOWED_TOOL_CALLS:
        return ALLOWED_TOOL_CALLS[tool_name]
    return set()
