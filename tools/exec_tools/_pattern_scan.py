"""Regex pattern scan for python_exec -- SECONDARY defense-in-depth layer.

WHY STRING MASKING (2026-07-14 refactor)
========================================
The legacy implementation ran its regexes over a hand-rolled ``_strip_comments()``
pass that ignored backslash escapes and triple quotes, so a ``#`` inside a string
literal corrupted the scanned text -- producing both false positives (legit code
blocked) and false negatives (payloads hidden in strings). That helper was dead
code but showed where the design was heading.

This module instead uses the standard-library ``tokenize`` module to replace every
STRING, FSTRING_* and COMMENT token with whitespace of EXACTLY the same length
(newlines preserved). Consequences:

  * regexes only ever see real code tokens -- string contents can neither trigger
    a rule nor hide one;
  * line/column numbers stay stable (same-length replacement), which keeps any
    future position-based reporting accurate;
  * if tokenization itself fails (unterminated string, bad encoding) the ORIGINAL
    source is scanned unchanged -- fail-safe: we never skip the scan.

KNOWN TRADE-OFF: on Python 3.12+ f-string expression parts are separate tokens and
get masked along with the literal text, so a pattern hidden inside an f-string
expression (e.g. ``f"{b.open('x')}"``) is not seen by THIS layer. That is accepted
because the AST layer (_ast_guard.py) sees those expressions as real nodes and its
hard floor blocks them; this layer remains pure defense-in-depth.

DELIVERY NOTE: same-package dependency of python_exec.py (delivered to CLIENTs via
tool_scanner + dynamic_loader). Imports only stdlib 'tokenize'/'re'. Policy data is
passed in by the caller -- zero sibling coupling, trivially unit-testable.
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
