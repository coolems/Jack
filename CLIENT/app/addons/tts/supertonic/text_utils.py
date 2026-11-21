"""Text sanitization + language auto-detection for the Supertonic engine.

sanitize_for_lang(text, lang) - per-language character fixes BEFORE synthesis.
  The German voice has a character whitelist and hard-fails on unsupported chars
  (discovered by testing: raw eszett and curly quotes raise
  "Found 1 unsupported character(s)"). The fixes are phonetically identical:
    de: eszett->ss, curly quotes->straight, en/em-dash->hyphen, ellipsis->...
    ro: a/â/î/s/ț diacritics stripped (a, a, i, s, t) - the engine wants plain ASCII
  Everything else passes through untouched.

detect_language(text, fallback) - character-fingerprint scan (microseconds, no ML):
  Romanian diacritics (a/â/î/s/ț) exist ONLY in Romanian -> 100% certain.
  German: eszett is German-only; umlauts (ae/oe/ue) are ambiguous in the world's
  languages (Swedish/Danish) but NEVER occur in English or Romanian, so within our
  en/de/ro set a single umlaut already means German. Everything else -> fallback.
"""

import re
from typing import List

_RO_CHARS = set("âîșț")
_DE_UMLAUTS = set("äöü")

#: (pattern, replacement) per language - applied in order, case preserved where noted.
_FIXES = {
    "de": [
        ("ß", "ss"),
        ("„", '"'),
        ("“", '"'),
        ("”", '"'),
        ("‘", "'"),
        ("’", "'"),
        ("–", "-"),
        ("—", "-"),
        ("…", "..."),
    ],
    "ro": [
        ("ă", "a"),
        ("â", "a"),
        ("î", "i"),
        ("ș", "s"),
        ("ş", "s"),
        ("ț", "t"),
        ("ţ", "t"),
    ],
    "en": [
        ("„", '"'),
        ("“", '"'),
        ("”", '"'),
        ("‘", "'"),
        ("’", "'"),
        ("–", "-"),
        ("—", "-"),
        ("…", "..."),
    ],
}


def sanitize_for_lang(text: str, lang: str) -> str:
    """Apply the per-language character fixes, then collapse whitespace."""
    if not text:
        return ""
    for pat, rep in _FIXES.get(lang, _FIXES["en"]):
        text = text.replace(pat, rep)
    # Normalize line breaks / tabs to spaces; keep the sentence punctuation as-is.
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    return text.strip()


def detect_language(text: str, fallback: str = "en") -> str:
    """Character-fingerprint detection for en/de/ro (see module docstring)."""
    if not text:
        return fallback
    sample = text[:2000]  # fingerprints appear early; cap the scan
    if any(c in _RO_CHARS for c in sample):
        return "ro"
    if "ß" in sample or any(c in _DE_UMLAUTS for c in sample):
        return "de"
    return fallback


def split_into_sentences(text: str) -> List[str]:
    """Split on sentence boundaries (period/!/? followed by space or end).

    Supertonic handles one sentence per call best; long texts are chunked here so
    the first audio bytes arrive fast (the old pool.py's job, inlined - playback
    now happens in the browser, so no server-side queue is needed).
    """
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    out: List[str] = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out or [text]
