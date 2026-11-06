"""Pure literal-text edit logic shared by the edit_tools modules (no imports, no path work).

WHY A SEPARATE MODULE
=====================
The CLIENT does not import tool files from disk - the SERVER ships each tool's source and the
CLIENT exec()s it (CLIENT/tools/remote_tools/loader). A tool's same-package imports are detected
by app/providers/coolems/tool_scanner/same_package_imports.py, and the sibling module is delivered
as a 'shared_dep' whose public symbols are merged into the tool's exec globals. That pipeline
only works for modules that need NOTHING at exec time, so this core is deliberately import-free:
pure string functions, no stdlib, no path/security work (the tool modules own those guards).

LINE-ENDING CONTRACT
====================
Every function here works on an LF view. The tools normalize the file content to LF first
(utils.normalize_line_endings), run the edit here, then re-apply the file's policy (LF, or CRLF
for .bat/.cmd) before writing. Matching stays whitespace-simple and the written bytes stay exact.

MATCHING CONTRACT
=================
Literal text only (never a regex), a match must be unique unless the caller asks for
replace_all, and an empty replacement deletes text.

BACKUP CONTRACT
===============
copy_backup() is the one byte-level helper here: it snapshots a file to '<file>.backup' before a
mutation. It uses plain open() binary I/O rather than shutil.copy2 because shutil is
dangerous-by-default in the remote-tool AST guard (CLIENT/tools/remote_tools/loader/ast_guard.py)
and is allowlisted per tool name only - a tool that patches tool sources must not depend on it.
Byte-exact copies keep the original newline convention intact in the backup.
"""

# Cap for the LCS table in changed_region(): the changed middle is normally a handful of
# lines, but a pathological edit could make rows*cols huge, so it degrades to a plain listing.
_LCS_CELL_BUDGET = 200_000


def copy_backup(filepath: str) -> None:
    """Copy *filepath* to '<filepath>.backup' before mutating it.

    Byte-exact binary copy, so the backup keeps the file's original newline convention.
    Plain open() I/O instead of shutil.copy2 - see the BACKUP CONTRACT above.
    """
    with open(filepath, "rb") as src:
        payload = src.read()
    with open(filepath + ".backup", "wb") as dst:
        dst.write(payload)


def find_matches(content: str, search: str) -> list:
    """Return every offset where *search* occurs in *content*, left to right, non-overlapping.

    Args:
        content: The LF view of the file.
        search: Literal text to find. An empty search never matches.

    Returns:
        List of int offsets (match starts); [] when there is no match.
    """
    if not search:
        return []
    offsets = []
    cursor = 0
    while True:
        found = content.find(search, cursor)
        if found < 0:
            return offsets
        offsets.append(found)
        cursor = found + len(search)
    return offsets


def line_number_at(content: str, offset: int) -> int:
    """Return the 1-based line number that holds *offset*."""
    line = 1
    for ch in content[:offset]:
        if ch == "\n":
            line += 1
    return line


def split_lines(content: str) -> list:
    """Split *content* into lines, without the empty entry a trailing newline produces."""
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def count_lines(content: str) -> int:
    """Number of real lines in *content* (a trailing newline does not add a line)."""
    return len(split_lines(content))


def splice(content: str, offsets: list, search_len: int, replacement: str) -> str:
    """Replace every match of length *search_len* at *offsets* with *replacement*.

    *offsets* are ascending, non-overlapping match starts (what find_matches() returns).
    An empty *replacement* deletes the matched text.
    """
    if not offsets:
        return content
    parts = []
    prev = 0
    for offset in offsets:
        parts.append(content[prev:offset])
        parts.append(replacement)
        prev = offset + search_len
    parts.append(content[prev:])
    return "".join(parts)


def insert_text(content: str, after_line: int, text: str) -> str:
    """Insert *text* as new line(s) after the 1-based line *after_line* (0 = at the start).

    Works on the LF view; the caller re-applies the file's line-ending policy afterwards.
    *text* is closed with a newline when it does not already end with one, so the inserted
    block is always a real line instead of a prefix of the following line. A trailing newline
    the file already had is preserved.
    """
    lines = split_lines(content)
    if text and not text.endswith("\n"):
        text = text + "\n"
    if after_line <= 0:
        return text + content

    head = "\n".join(lines[:after_line])
    tail = "\n".join(lines[after_line:])
    trailing = "\n" if content.endswith("\n") else ""
    if tail:
        return head + "\n" + text + tail + trailing
    return head + "\n" + text + trailing


def changed_region(old: str, new: str, max_lines: int = 12) -> str:
    """Compact '-'/'+ report of the changed LINES between *old* and *new*.

    Identical leading and trailing lines are stripped, then the edited middle is aligned with a
    longest-common-subsequence pass, so unchanged lines inside it appear as plain context and
    only real additions/removals carry a sign. Output is capped to *max_lines* lines and the
    remainder is summarised, so tool results stay small. Returns "" when nothing changed.
    """
    if old == new:
        return ""

    old_lines = split_lines(old)
    new_lines = split_lines(new)
    limit = min(len(old_lines), len(new_lines))

    lead = 0
    while lead < limit and old_lines[lead] == new_lines[lead]:
        lead += 1
    tail = 0
    while (tail < limit - lead
           and old_lines[len(old_lines) - 1 - tail] == new_lines[len(new_lines) - 1 - tail]):
        tail += 1

    old_mid = old_lines[lead:len(old_lines) - tail]
    new_mid = new_lines[lead:len(new_lines) - tail]

    lines, consumed = _align_middle(old_mid, new_mid, max_lines)
    omitted = (len(old_mid) + len(new_mid)) - consumed
    if omitted > 0:
        lines.append(f"  ... {omitted} more changed line(s) not shown")
    return "\n".join(lines)


def _align_middle(old_mid: list, new_mid: list, max_lines: int):
    """Return (display_lines, lines_consumed) for the changed middle region.

    'lines_consumed' counts old+new lines actually represented, so the caller can report how
    many were left out by the cap.
    """
    if len(old_mid) * len(new_mid) > _LCS_CELL_BUDGET:
        # Degraded mode: too large for the DP table - list both sides without alignment.
        lines = [f"  - {line}" for line in old_mid[:max_lines]]
        lines += [f"  + {line}" for line in new_mid[:max_lines - len(lines)]]
        return lines, len(lines)

    rows = len(old_mid) + 1
    cols = len(new_mid) + 1
    table = [[0] * cols for _ in range(rows)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            if i < rows - 1 and j < cols - 1 and old_mid[i] == new_mid[j]:
                table[i][j] = table[i + 1][j + 1] + 1
            else:
                down = table[i + 1][j] if i < rows - 1 else 0
                right = table[i][j + 1] if j < cols - 1 else 0
                table[i][j] = max(down, right)

    lines = []
    consumed = 0
    i = j = 0
    while i < len(old_mid) or j < len(new_mid):
        if len(lines) >= max_lines:
            return lines, consumed
        if i < len(old_mid) and j < len(new_mid) and old_mid[i] == new_mid[j]:
            if table[i + 1][j + 1] >= table[i + 1][j] and table[i + 1][j + 1] >= table[i][j + 1]:
                lines.append(f"  {old_mid[i]}")   # unchanged context line
                i += 1
                j += 1
                consumed += 2
                continue
        if i < len(old_mid) and (j >= len(new_mid) or table[i + 1][j] >= table[i][j + 1]):
            lines.append(f"  - {old_mid[i]}")
            i += 1
            consumed += 1
        else:
            lines.append(f"  + {new_mid[j]}")
            j += 1
            consumed += 1
    return lines, consumed
