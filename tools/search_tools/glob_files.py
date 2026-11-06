"""glob_files - list FILES by name pattern inside working_root, newest first.

Ported from deepseek-harness's `packages/fs/tool-fs-search` glob tool (Tier-1 item 1 in
the tool comparison, 2026-11-06). Named glob_files rather than glob for two reasons: the
CLIENT loader blocks 'import glob' (ast_guard.DANGEROUS_MODULES) and globals_builder
injects delivered modules by short name into every tool's exec globals, so a module named
'glob' would shadow that name repo-wide.

Backed by `rg --files --glob=<pattern> --sort=modified`: one spawn lists the whole tree,
respects .gitignore by default, and skips binary/oversized files on its own - so no manual
walk, size cap or binary sniff is needed here.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "glob_files",
        "description": "List files whose names match a glob pattern inside working_root, sorted newest-first. ALL paths are locked inside working_root - absolute paths outside working_root are NOT allowed. path is a DIRECTORY only (relative to working_root; empty = working_root); the search is recursive. pattern is a glob such as '*.py', '**/*.json', 'tools/**/*.md'; it is matched relative to the directory being searched (path), so '*.py' with path='tools/search_tools' lists that folder's files while 'tools/**/*.py' with an empty path scopes to tools/. Gitignored folders are skipped by default; include_ignored=true also lists them (heavy folders like venv/ and model weights stay excluded either way). Returns paths relative to working_root, capped, with the full list spilled to .temp/spill/ when capped.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern for file names/paths. Examples: '*.py', '*.md', 'tests/test_*.py', '**/*.json'."
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search, relative to working_root. Empty string = whole working_root. Use dot or root for working_root. NEVER a file path.",
                    "default": ""
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "Also list gitignored/hidden files. Heavy folders (venv, node_modules, .git, model weights, runtimes) stay excluded regardless. Default false.",
                    "default": False
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum paths in the result (1-100). Beyond it the full list is saved to .temp/spill/. Default 100.",
                    "default": 100
                }
            },
            "required": ["pattern"]
        }
    }
}

import logging
import os
import subprocess

from ..utils import get_working_root, resolve_path_to_dir
from ._rg_runtime import ensure_binary
from ._search_core import (DEADLINE_S, MAX_PATHS, build_glob_argv, format_paths,
                           relative_to, write_spill)

logger = logging.getLogger("COOLEMS.Tools.Search")


def glob_files(pattern: str, path: str = "", include_ignored: bool = False,
               max_results: int = MAX_PATHS) -> str:
    """List files matching a glob inside working_root, newest first.

    SECURITY RULE - CRITICAL:
        * path = DIRECTORY only (e.g. 'tools'), resolved against working_root.
        * '..' traversal and absolute paths outside working_root are refused.

    Args:
        pattern: Glob for file names/paths ('*.py', '**/*.json').
        path: Directory to search (empty = working_root).
        include_ignored: Include gitignored/hidden files (heavy folders stay excluded).
        max_results: Cap for the returned list (1-100).

    Returns:
        'Found N file(s) matching ... (newest first)' followed by one path per line, or
        'No files match ...', or 'Error: ...' carrying the reason.
    """
    logger.info(f"[glob_files] {pattern!r} in {path!r} ignored={include_ignored}")

    try:
        if not isinstance(pattern, str) or not pattern.strip():
            return "Error: pattern is required and must be a non-empty string."
        if not isinstance(path, str):
            return "Error: path must be a string."
        try:
            cap = int(max_results)
        except (TypeError, ValueError):
            return "Error: max_results must be an integer (1-100)."
        cap = max(1, min(cap, MAX_PATHS))

        effective_working = get_working_root()
        dirpath = resolve_path_to_dir(effective_working, path)
        if not os.path.isdir(dirpath):
            return f"Error: '{path}' is not a directory inside working_root - " \
                   f"use list_files to confirm the path (glob_files takes a DIRECTORY)."

        argv = build_glob_argv(pattern, dirpath, bool(include_ignored))
        exe = ensure_binary()
        try:
            # cwd=dirpath (same convention as git_tools/git_clone.py: cwd=, never os.chdir,
            # which would mutate the process-wide CWD shared by every other coroutine).
            # rg anchors --glob patterns to the process CWD, and this process's CWD is not
            # necessarily working_root (CLIENT/code_client.py chdirs into CLIENT/), so
            # without this a pattern scoped to the searched directory silently returned
            # nothing. Patterns are therefore read relative to the directory being searched.
            r = subprocess.run([exe, *argv], capture_output=True, text=True,
                               timeout=DEADLINE_S, cwd=dirpath)
        except subprocess.TimeoutExpired:
            return f"Error: file listing timed out after {DEADLINE_S}s - narrow the pattern or the path."
        except OSError as e:
            return f"Error: ripgrep could not run ({e})."

        code = r.returncode
        if code == 1:
            return f"No files match {pattern!r} in {os.path.basename(dirpath)} (or its subfolders)."
        if code >= 2:
            detail = (r.stderr or r.stdout or "").strip()[:400]
            return f"Error: ripgrep failed (exit {code}): {detail or 'no detail'}"

        all_paths = [line for line in (r.stdout or "").splitlines() if line.strip()]
        total = len(all_paths)
        if total == 0:
            return f"No files match {pattern!r} in {os.path.basename(dirpath)} (or its subfolders)."

        # rg's --sort=modified is ASCENDING (oldest first) - verified against the pinned
        # binary. Reversed here so the result is newest-first, which is what a search for
        # "what did I just change" needs; the cap then keeps the newest entries.
        newest_first = list(reversed(all_paths))
        kept = newest_first[:cap]
        truncated = total > cap
        spill_note = ""
        if truncated:
            full_body = "\n".join(relative_to(effective_working, p) for p in newest_first)
            spill_note = write_spill("glob", full_body)

        return format_paths(kept, effective_working, pattern, total, truncated, spill_note)

    except ValueError as e:
        logger.warning(f"[glob_files] Security violation for path={path!r}: {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"[glob_files] Failed for {pattern!r} in {path!r}: {e}")
        return f"Error: glob_files failed: {e}"
