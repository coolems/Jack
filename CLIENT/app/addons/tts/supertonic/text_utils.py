"""Text sanitization + language auto-detection for the Supertonic engine.

sanitize_for_lang(text, lang) - per-language character fixes BEFORE synthesis.
  German (de) fix list (discovered by testing): eszett->ss, curly quotes->straight,
  en/em-dash->hyphen, ellipsis->...
  Romanian (ro) and English (en) pass through UNCHANGED. supertonic-3's own
  tokenizer supports the Romanian diacritics: its NFKD normalization decomposes
  ă/â/î -> a/i + combining breve/circumflex and ș/ț -> s/t + combining cedilla,
  and the text is wrapped in the <ro> token, so the model pronounces them correctly.
  Stripping the diacritics (the old code) forced plain ASCII - the "Romanian
  voices are missing their diacritics" bug.

detect_language(text, fallback) - character-fingerprint scan (microseconds, no ML):
  Romanian diacritics (ă/â/î/ș/ț, incl. legacy cedilla ş/ţ) exist ONLY in Romanian.
  German: eszett is German-only; umlauts (ä/ö/ü) are ambiguous in the world's
  languages (Swedish/Danish) but NEVER occur in English or Romanian, so within our
  en/de/ro set a single umlaut already means German. Everything else -> fallback.
"""

import re
from typing import List

#: Romanian diacritics (composed + legacy cedilla + uppercase). Exists ONLY in ro.
_RO_CHARS = set("ăâîșțĂÂÎȘȚşţŞŢ")
_DE_UMLAUTS = set("äöü")

#: supertonic-2's unicode indexer has NO Romanian diacritics (verified against its
#: unicode_indexer.json: ă/â/î/ș/ț all map to -1). supertonic-3 tokenizes them via
#: NFKD (combining breve/circumflex/cedilla are in its indexer). So when the model
#: is supertonic-2 we rewrite diacritics to plain letters - a phonetic fallback
#: (Romanian is respelled the same way in ASCII anyway: "să" -> "sa").
_RO_FALLBACK = {
    "ă": "a", "â": "a", "î": "i", "ș": "s", "ț": "t",
    "Ă": "A", "Â": "A", "Î": "I", "Ș": "S", "Ț": "T",
    "ş": "s", "ţ": "t", "Ş": "S", "Ţ": "T",
}

#: (pattern, replacement) per language - applied in order, case preserved where noted.
#: "ro" is intentionally ABSENT: it falls back to the neutral "en" fixes (curly
#: quotes/dashes) without ever stripping the diacritics.
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


def sanitize_for_lang(text: str, lang: str, model: str = "supertonic-3") -> str:
    """Apply the per-language character fixes, then collapse whitespace.

    Romanian and English text keeps its diacritics - supertonic-3 tokenizes them
    correctly (see module docstring); only German needs the phonetic rewrite list.
    supertonic-2 has no Romanian diacritics in its indexer, so for that model the
    diacritics are respelled to plain letters (phonetic, ASCII-safe).
    """
    if not text:
        return ""
    for pat, rep in _FIXES.get(lang, _FIXES["en"]):
        text = text.replace(pat, rep)
    if model == "supertonic-2":
        text = "".join(_RO_FALLBACK.get(c, c) for c in text)
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
