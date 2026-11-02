"""Tests: the three DNA JSON copies must stay byte-identical (2026-11-14, code-review C3).

Jack's identity files (identity.json, intelligence.json, lessons.json) exist in three
places on purpose:

  tools/dna/            - SERVER-side copy (the agent's own DNA)
  CLIENT/app/dna/       - CLIENT-side copy (delivered code reads it via app.dna)
  CLIENT/tools/app/dna/ - copy inside the delivered tools tree (remote_tools import path)

They are NOT merged into one file: the delivered CLIENT code needs its own copies, so a
SERVER-only single source would break the client at runtime. What must hold instead is
that the three copies never drift apart - editing one DNA file without the other two
gives the SERVER and the CLIENT different personalities/lessons.

Run:  python -m pytest tools/test_tools/test_dna_json_copies_identical.py -v
"""

import hashlib
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))

DNA_DIRS = [
    os.path.join(_REPO_ROOT, "tools", "dna"),
    os.path.join(_REPO_ROOT, "CLIENT", "app", "dna"),
    os.path.join(_REPO_ROOT, "CLIENT", "tools", "app", "dna"),
]

DNA_JSON_FILES = ["identity.json", "intelligence.json", "lessons.json"]


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def test_all_dna_dirs_exist():
    for d in DNA_DIRS:
        assert os.path.isdir(d), f"missing DNA directory: {d}"


def test_every_copy_present_in_every_dir():
    for d in DNA_DIRS:
        for fname in DNA_JSON_FILES:
            path = os.path.join(d, fname)
            assert os.path.isfile(path), f"missing {fname} in {d}"


def test_dna_json_copies_are_byte_identical():
    """All three copies of each DNA JSON must be byte-for-byte the same."""
    for fname in DNA_JSON_FILES:
        blobs = {}
        for d in DNA_DIRS:
            blobs[d] = _read_bytes(os.path.join(d, fname))
        digests = {d: _sha(b) for d, b in blobs.items()}
        unique = set(digests.values())
        if len(unique) != 1:
            detail = "\n".join(f"  {d}: {digests[d]} ({len(blobs[d])} bytes)" for d in DNA_DIRS)
            raise AssertionError(
                f"{fname} drifted between the DNA copies (edit all three together):\n{detail}"
            )


def test_dna_json_files_parse_as_json():
    """Guard against a truncated/edited copy that no longer parses."""
    for d in DNA_DIRS:
        for fname in DNA_JSON_FILES:
            path = os.path.join(d, fname)
            raw = _read_bytes(path).decode("utf-8")
            try:
                json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AssertionError(f"{path} is not valid JSON: {exc}")


if __name__ == "__main__":
    for fn in (
        test_all_dna_dirs_exist,
        test_every_copy_present_in_every_dir,
        test_dna_json_copies_are_byte_identical,
        test_dna_json_files_parse_as_json,
    ):
        fn()
    print("dna JSON copy checks passed")
