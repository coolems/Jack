"""Shared logic for the search tools (grep + glob_files) - NOT a tool itself.

Ported from deepseek-harness's `packages/fs/tool-fs-search`, which drives ripgrep through
a plain argv vector (never a shell string, so the pattern can never inject a command) and
parses its NDJSON output. Kept as a same-package dep so both tools share one argv builder,
one parser, one cap policy and one result format (the shape of tools/edit_tools/_edit_core.py).

Import policy: stdlib only, nothing from ast_guard.DANGEROUS_MODULES. The actual spawn
lives in the tool modules, where the auditable policy.py rows apply. Relative imports use
the delivered forms ('from ..utils import X'), which the CLIENT's source_transform rewrites
into injected-symbol lookups.
"""

import json
import logging
import os
import time

from ..utils import get_working_root

logger = logging.getLogger("COOLEMS.Tools.Search")

# Caps - mirrors the upstream tool's limits so one call can never flood the context.
MAX_MATCHES = 250           # matches kept in the tool result
MAX_LINE_CHARS = 2000       # characters kept per matched line
MAX_PATHS = 100             # paths kept by glob_files
DEADLINE_S = 30             # hard kill for one rg invocation
MAX_OUTPUT_BYTES = 20_000_000   # raw stdout ceiling before parsing

# Heavy folders that must never be searched, even with include_ignored=true. The repo
# gitignores them (venv, model weights, client runtimes), but --no-ignore would still read
# them - ~24 GB of GGUF/DLL churn. Negated as rg globs.
HEAVY_GLOBS = ["**/venv/**", "**/.venv/**", "**/node_modules/**", "**/.git/**",
               "**/llama_server/models/**", "**/runtimes/**", "**/__pycache__/**"]
VCS_GLOBS = ["**/.git", "**/.git/**"]


def relative_to(root: str, path: str) -> str:
    """Path as seen from working_root, so results are copyable into other tools."""
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def preview(text: str) -> str:
    """One line for the result: tabs expanded, trailing space dropped, capped."""
    line = text.replace("\t", "    ").rstrip()
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS - 3] + "..."
    return line


def truncate_output(text: str) -> str:
    """Cap rg's raw stdout before parsing so one call cannot eat the context.

    Truncation is cut at a line boundary: a half NDJSON line would be skipped by the
    parser anyway, and a partial trailing line could otherwise be counted as a match.
    """
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    cut = text[:MAX_OUTPUT_BYTES]
    nl = cut.rfind("\n")
    return cut[:nl + 1] if nl > 0 else ""


def build_grep_argv(pattern: str, target: str, include: str, context_lines: int,
                    ignore_case: bool, include_ignored: bool) -> list[str]:
    """argv for one grep run. --no-config first: a stray RIPGREP_CONFIG_PATH or rg.conf
    beside the binary could otherwise inject --pre and make rg run an arbitrary
    preprocessor on every matched file."""
    argv = ["--no-config", "--json"]
    if include_ignored:
        argv += ["--no-ignore", "--hidden"]
        argv += [f"--glob=!{g}" for g in HEAVY_GLOBS + VCS_GLOBS]
    if ignore_case:
        argv.append("--ignore-case")
    if context_lines:
        argv.append(f"--context={context_lines}")
    if include:
        argv.append(f"--glob={include}")
    argv += ["--regexp", pattern, "--", target]
    return argv


def build_glob_argv(pattern: str, target: str, include_ignored: bool) -> list[str]:
    """argv for one file-listing run (mtime-sorted, newest first)."""
    argv = ["--no-config", "--files", "--glob", pattern, "--sort=modified"]
    if include_ignored:
        argv += ["--no-ignore", "--hidden"]
        argv += [f"--glob=!{g}" for g in HEAVY_GLOBS + VCS_GLOBS]
    argv += ["--", target]
    return argv


def validate_include(include: str) -> str | None:
    """Return an error for an unusable --include value, else None.

    One positive glob only: '!' (negation) and comma lists are rejected rather than
    silently forwarded, because rg treats them differently and a comma would become a
    literal filename pattern.
    """
    if not include:
        return None
    if include.startswith("!"):
        return "Error: include must be a positive glob (e.g. '*.py'), not a negation."
    if "," in include:
        return "Error: include accepts ONE glob pattern, not a comma-separated list."
    return None


def _event_text(data: dict) -> str:
    """The matched/context line text from an rg JSON event.

    rg 15 nests the line under data['lines']['text']; older builds used a flat
    'line_text'. Both are accepted so the parser survives an rg upgrade or downgrade.
    """
    lines = data.get("lines")
    if isinstance(lines, dict):
        return lines.get("text") or ""
    return data.get("line_text") or ""


def parse_grep_json(raw: str, max_matches: int) -> tuple[list, int, bool, list[str]]:
    """Parse rg's NDJSON into [(path, [(kind, lineno, text)])] plus match stats.

    Returns (files, total_matches, truncated, errors). Every match is COUNTED even after
    the cap, so the header can say 'kept K of N'. A malformed line is skipped instead of
    raising; a per-file rg error (unreadable path) becomes a note, not a crash.
    """
    files: list = []
    current_path: str | None = None
    rows: list = []
    total = 0
    truncated = False
    errors: list[str] = []

    def flush() -> None:
        nonlocal current_path, rows
        if current_path is not None and rows:
            files.append((current_path, rows))
        current_path, rows = None, []

    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        data = ev.get("data") or {}
        if kind == "begin":
            flush()
            current_path = (data.get("path") or {}).get("text") or ""
        elif kind == "match":
            total += 1
            if total > max_matches:
                truncated = True
                continue
            rows.append(("match", int(data.get("line_number") or 0),
                         preview(_event_text(data))))
        elif kind == "context":
            rows.append(("context", int(data.get("line_number") or 0),
                         preview(_event_text(data))))
        elif kind == "end":
            flush()
        elif kind == "summary":
            err = data.get("error")
            if err:
                errors.append(f"{(data.get('path') or {}).get('text', '?')}: {err}")
            flush()

    flush()
    return files, total, truncated, errors


def format_matches(files: list, root: str, pattern: str, total: int, kept: int,
                   truncated: bool, errors: list[str], spill_note: str = "") -> str:
    """Body: a 'Found N matches' header, then per-file groups with line numbers."""
    if total == 0:
        return f"No matches for {pattern!r}."
    head = (f"Found {kept} of {total} matches for {pattern!r}" if truncated
            else f"Found {total} match(es) for {pattern!r}")
    blocks = [head]
    for path, rows in files:
        blocks.append(f"\n{relative_to(root, path)}")
        for kind, lineno, text in rows:
            blocks.append(f"  Line {lineno}: {text}" if kind == "match" else f"  {lineno} | {text}")
    if errors:
        blocks.append("\nSkipped: " + "; ".join(errors[:10]))
    if truncated:
        blocks.append(f"\nCapped at {kept} matches - narrow the pattern or the path.")
    if spill_note:
        blocks.append(spill_note)
    return "\n".join(blocks)


def format_paths(paths: list[str], root: str, pattern: str, total: int,
                 truncated: bool, spill_note: str = "") -> str:
    """Body: newest-first file list, paths relative to working_root."""
    if total == 0:
        return f"No files match {pattern!r}."
    head = (f"Found {len(paths)} of {total} files matching {pattern!r}" if truncated
            else f"Found {total} file(s) matching {pattern!r} (newest first)")
    body = [head] + [relative_to(root, p) for p in paths]
    if truncated:
        body.append(f"\nCapped at {len(paths)} paths - narrow the pattern or the path.")
    if spill_note:
        body.append(spill_note)
    return "\n".join(body)


def write_spill(name: str, body: str) -> str:
    """Save a capped result under .temp/spill/ and return a locator line.

    Repo rule keeps scratch work in .temp, so the full list stays retrievable without
    polluting the tree. Returns '' when the write fails - a spill must never break a
    tool result.
    """
    try:
        root = get_working_root()
        spill_dir = os.path.join(root, ".temp", "spill")
        os.makedirs(spill_dir, exist_ok=True)
        path = os.path.join(spill_dir, f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return f"Full result saved to {relative_to(root, path)} (read_file it for the rest)."
    except Exception as e:
        logger.warning("[search] spill write failed: %s", e)
        return ""
