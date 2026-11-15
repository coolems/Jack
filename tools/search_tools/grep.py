"""grep - search file CONTENTS inside working_root with ripgrep, with line numbers.

Ported from deepseek-harness's `packages/fs/tool-fs-search` (Tier-1 item 1 in the tool
comparison, 2026-11-06). Before this tool existed, content search meant reading files one
by one - the biggest gap in my toolset. ripgrep 15 is spawned as a plain argv vector
(never a shell string), so a pattern can never inject a command, and its NDJSON output
gives real line numbers plus optional context lines.

WHY THE SPAWN LIVES HERE AND NOT IN _search_core.py: the CLIENT loader AST-validates the
TOOL source against policy.py's per-tool allowlist, but shared deps are exec'd WITHOUT
that validation. Keeping subprocess.run in this file means the trust decision stays
auditable per tool (see CLIENT/tools/remote_tools/loader/policy.py).

Exit codes are ripgrep's contract: 0 = matches found, 1 = zero matches (NOT an error),
>=2 = real failure (bad pattern, unreadable path, etc.).
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": "Search file contents inside working_root with ripgrep; matches come with file paths and line numbers. Gitignored and heavy folders (venv, node_modules, .git, model weights, runtimes) are skipped unless include_ignored=true. Capped results spill to .temp/spill/ with a locator.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression (ripgrep/PCRE2). Examples: 'def get_working_root', 'TODO|FIXME', 'class\\s+\\w+Tools'."
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search (recursive); '' / '.' / 'root' = whole working_root.",
                    "default": ""
                },
                "include": {
                    "type": "string",
                    "description": "One glob limiting which files are searched ('*.py', '*.md'), relative to the searched directory. Negations and comma lists are rejected.",
                    "default": ""
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Context lines before/after each match (0-10). Default 0.",
                    "default": 0
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive match. Default false.",
                    "default": False
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "Also search gitignored/hidden files. Heavy folders stay excluded regardless. Default false.",
                    "default": False
                },
                "max_matches": {
                    "type": "integer",
                    "description": "Max matches returned (1-250); the full list is spilled when capped. Default 250.",
                    "default": 250
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
from ._search_core import (DEADLINE_S, MAX_MATCHES, build_grep_argv, format_matches,
                           parse_grep_json, truncate_output, validate_include, write_spill)

logger = logging.getLogger("COOLEMS.Tools.Search")

MAX_CONTEXT_LINES = 10
SPILL_PARSE_LIMIT = 50_000   # parse ceiling for the spill copy of a capped result


def grep(pattern: str, path: str = "", include: str = "", context_lines: int = 0,
         ignore_case: bool = False, include_ignored: bool = False,
         max_matches: int = MAX_MATCHES) -> str:
    """Search file contents inside working_root; returns matches with line numbers.

    SECURITY RULE - CRITICAL:
        * path = DIRECTORY only (e.g. 'tools/search_tools'), resolved against working_root.
        * '..' traversal and absolute paths outside working_root are refused.

    Args:
        pattern: ripgrep regular expression.
        path: Directory to search (empty = working_root).
        include: One file-type glob ('*.py', '*.md').
        context_lines: 0-10 context lines around each match.
        ignore_case: Case-insensitive search.
        include_ignored: Include gitignored/hidden files (heavy folders stay excluded).
        max_matches: Cap for the returned result (1-250).

    Returns:
        'Found N match(es) for ...' with per-file groups of 'Line N: text', or
        'No matches ...', or 'Error: ...' carrying the reason.
    """
    logger.info(f"[grep] {pattern!r} in {path!r} include={include!r} ctx={context_lines} "
                f"ic={ignore_case} ignored={include_ignored}")

    try:
        if not isinstance(pattern, str) or not pattern.strip():
            return "Error: pattern is required and must be a non-empty string."
        if not isinstance(path, str) or not isinstance(include, str):
            return "Error: path and include must be strings."
        try:
            ctx = int(context_lines)
        except (TypeError, ValueError):
            return "Error: context_lines must be an integer (0-10)."
        if ctx < 0 or ctx > MAX_CONTEXT_LINES:
            return f"Error: context_lines must be between 0 and {MAX_CONTEXT_LINES}."
        try:
            cap = int(max_matches)
        except (TypeError, ValueError):
            return "Error: max_matches must be an integer (1-250)."
        cap = max(1, min(cap, MAX_MATCHES))

        include_err = validate_include(include)
        if include_err:
            return include_err

        effective_working = get_working_root()
        dirpath = resolve_path_to_dir(effective_working, path)
        if not os.path.isdir(dirpath):
            return f"Error: '{path}' is not a directory inside working_root - " \
                   f"use list_files to confirm the path (grep takes a DIRECTORY, not a file)."

        argv = build_grep_argv(pattern, dirpath, include, ctx, bool(ignore_case),
                               bool(include_ignored))
        exe = ensure_binary()
        try:
            # cwd=dirpath (same convention as git_tools/git_clone.py: cwd=, never os.chdir,
            # which would mutate the process-wide CWD shared by every other coroutine).
            # rg anchors --glob patterns to the process CWD, and this process's CWD is not
            # necessarily working_root (CLIENT/code_client.py chdirs into CLIENT/), so
            # without this an include='*.py' or a heavy-dir negation would be matched
            # against the wrong base and silently return nothing.
            # encoding/errors are EXPLICIT, never left to the locale default: on Windows
            # text=True decodes with cp1252, and ripgrep emits UTF-8 (emoji in .html/.md,
            # non-Latin text in logs). A cp1252 decode raised UnicodeDecodeError inside
            # subprocess's own reader thread, so stdout came back EMPTY and grep reported
            # "No matches" for a search that actually had hits. utf-8 + replace keeps every
            # byte readable and never fails the call.
            r = subprocess.run([exe, *argv], capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=DEADLINE_S, cwd=dirpath)
        except subprocess.TimeoutExpired:
            return f"Error: search timed out after {DEADLINE_S}s - narrow the pattern or the path."
        except OSError as e:
            return f"Error: ripgrep could not run ({e})."

        code = r.returncode
        if code == 1:
            return f"No matches for {pattern!r} in {os.path.basename(dirpath)} (or its subfolders)."
        if code >= 2:
            detail = (r.stderr or r.stdout or "").strip()[:400]
            return f"Error: ripgrep failed (exit {code}): {detail or 'no detail'}"

        raw = truncate_output(r.stdout or "")
        files, total, truncated, errors = parse_grep_json(raw, cap)
        kept = sum(len(rows) for _p, rows in files)

        spill_note = ""
        if truncated:
            full_files, full_total, _t, full_errs = parse_grep_json(raw, SPILL_PARSE_LIMIT)
            full_body = format_matches(full_files, effective_working, pattern, full_total,
                                       full_total, False, full_errs)
            spill_note = write_spill("grep", full_body)

        return format_matches(files, effective_working, pattern, total, kept,
                              truncated, errors, spill_note)

    except ValueError as e:
        logger.warning(f"[grep] Security violation for path={path!r}: {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"[grep] Failed for {pattern!r} in {path!r}: {e}")
        return f"Error: grep failed: {e}"
