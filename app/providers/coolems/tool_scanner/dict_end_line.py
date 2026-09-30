"""String-aware brace matching for top-level dict extraction."""

def _find_dict_end_line(file_lines, s_line):
    """L7 fix: string-aware brace matching to find the line where a top-level dict ends.

    The old fallback counted every '{' / '}' character, so braces inside string
    literals (e.g. a description containing "use {name} here") broke the count and
    truncated or mis-framed the extracted snippet. This walker tracks whether it is
    inside a single- or double-quoted string (with backslash escapes) and only counts
    braces outside strings. Triple-quoted strings are handled by treating them as an
    ordinary quoted run until their matching triple quote closes.

    Returns the 1-past-end line index (e_line such that file_lines[s_line:e_line] is
    the dict), or None if the braces never balance.
    """
    brace_count = 0
    seen_open = False
    in_str = None      # current quote char ('"', "'", or triple variant) when inside a string
    i = s_line
    while i < len(file_lines):
        line = file_lines[i]
        j = 0
        n = len(line)
        while j < n:
            ch = line[j]
            if in_str is not None:
                if ch == '\\':
                    j += 2      # skip escaped char (covers \" inside strings)
                    continue
                if in_str in ('"""', "'''") :
                    if line.startswith(in_str, j):
                        in_str = None
                        j += 3
                        continue
                elif ch == in_str:
                    in_str = None
                j += 1
                continue
            # not inside a string
            if line.startswith('"""', j) or line.startswith("'''", j):
                in_str = line[j:j+3]
                j += 3
                continue
            if ch in ('"', "'"):
                in_str = ch
                j += 1
                continue
            if ch == '{':
                brace_count += 1
                seen_open = True
            elif ch == '}':
                brace_count -= 1
                if seen_open and brace_count == 0:
                    return i + 1
            j += 1
        # line ended inside a string — keep scanning (multi-line strings)
        i += 1
    return None
