"""
Text sanitization and sentence splitting for TTS.

Two public functions:
  - sanitize_text(text)   → clean string (letters, numbers, spaces, periods, commas, apostrophes, hyphens)
  - split_into_sentences(text) → list of clean sentences, each ending with '.'
"""

import re
from typing import List


def sanitize_text(text: str) -> str:
    """
    Sanitize text: keep letters (including accented/Unicode), numbers,
    spaces, periods, commas, apostrophes, and hyphens.
    Everything else is replaced with a comma.
    Consecutive commas are collapsed into a single comma.
    Trailing commas are stripped.
    """
    if not text:
        return ""

    # Replace any character that is NOT a letter, number, period, space, comma, apostrophe, or hyphen with a comma.
    # \w with UNICODE flag matches letters + digits + underscore.
    # We explicitly allow comma, apostrophe, and hyphen too.
    # Hyphen is at the end of the class so it's treated as literal.
    sanitized = re.sub(r"[^\w\s.,'-]", ",", text, flags=re.UNICODE)

    # Remove underscores (\w includes them but we don't want them)
    sanitized = sanitized.replace("_", " ")

    # Normalize whitespace (multiple spaces/newlines → single space)
    sanitized = re.sub(r"\s+", " ", sanitized)

    # Collapse multiple consecutive commas into a single comma
    sanitized = re.sub(r",+", ",", sanitized)

    # Strip trailing commas (these are the ones from !? etc. at end of sentence)
    sanitized = sanitized.rstrip(",")

    # Strip leading/trailing whitespace only
    sanitized = sanitized.strip()

    return sanitized


def split_into_sentences(text: str) -> List[str]:
    """
    Split text into individual sentences.

    Calls sanitize_text first, then splits on periods.
    Each returned sentence ends with a period.
    """
    sanitized = sanitize_text(text)
    if not sanitized:
        return []

    # Split on period followed by space (avoids splitting decimals like 99.99)
    sentences = []
    for s in sanitized.split(". "):
        s = s.strip()
        if s:
            if not s.endswith("."):
                s = s + "."
            sentences.append(s)
    return sentences
