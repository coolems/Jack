"""Regex pattern scan for python_exec -- SECONDARY defense-in-depth layer.

The regexes run over a MASKED copy of the source: the standard-library ``tokenize``
module replaces every STRING, FSTRING_* and COMMENT token with whitespace of EXACTLY the
same length, newlines preserved. That gives:
  * a ``#`` or quote inside a string cannot corrupt the scanned text, so no false
    positives on legitimate code and no payloads hidden inside strings;
  * stable offsets and line numbers (same-length replacement) for position reporting;
  * on tokenization failure (unterminated string, bad encoding) the ORIGINAL source is
    scanned unchanged - the scan is never skipped.

TRADE-OFF: on Python 3.12+ f-string expression parts are separate tokens and get masked,
so a pattern inside an f-string expression is invisible to THIS layer. _ast_guard.py sees
those expressions as real nodes and its hard floor blocks them, so this layer stays pure
defense-in-depth.

DELIVERY: same-package dependency of python_exec.py (sent to CLIENTs via tool_scanner +
dynamic_loader). Stdlib only ('tokenize', 're'); policy data comes from the caller, so
the module has no sibling coupling and is trivially unit-testable.
"""

import re
import tokenize
from io import StringIO


def _blank_preserving_newlines(text):
    """Whitespace of the same length as *text*, keeping newlines at their positions."""
    return "".join(ch if ch == "\n" else " " for ch in text)


def mask_strings(code):
    """Replace every string/fstring/comment token with same-length whitespace.

    Returns a version of *code* where all non-code text is blanked out but the
    overall length and line structure are preserved, so regex matches (and any
    offsets derived from them) stay valid. Falls back to the original source if
    tokenization fails -- the scan must never be skipped silently.

    Args:
        code: Python source text to mask.

    Returns:
        Masked source string (same length as input).
    """
    try:
        tokens = list(tokenize.generate_tokens(StringIO(code).readline))
    except (tokenize.TokenizeError, IndentationError, SyntaxError, UnicodeDecodeError):
        return code  # fail-safe: scan the original rather than skip

    # Token types whose text is data, not code. FSTRING_* exist only on 3.12+;
    # getattr with a sentinel keeps this module importable and correct everywhere.
    masked_types = {tokenize.STRING, tokenize.COMMENT}
    for _name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        const = getattr(tokenize, _name, None)
        if isinstance(const, int):
            masked_types.add(const)

    return "".join(
        _blank_preserving_newlines(tok.string) if tok.type in masked_types else tok.string
        for tok in tokens
    )


def run_pattern_scan(code, policy):
    """Scan code for forbidden patterns (SECONDARY regex fallback).

    Args:
        code: user-supplied Python source.
        policy: namespace with FORBIDDEN_PATTERNS from _policy.py.

    Returns:
        (has_violations, violations_list) -- same contract as the legacy
        _check_forbidden_patterns().
    """
    violations = []
    masked = mask_strings(code)
    for pattern, message in policy.FORBIDDEN_PATTERNS.items():
        if re.search(pattern, masked):
            violations.append(message)
    return len(violations) > 0, violations
