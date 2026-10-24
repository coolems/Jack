"""Canonical model identity used for routing, queueing and profile authorization.

2026-10 fleet: a llama request is routed to the MODEL GROUP it asks for. The identity
must be identical everywhere it is derived from, otherwise a request would never meet
its nodes:

  * CLIENT picks a model from the list the SERVER sent (a GGUF filename)
  * SERVER profile authorization lists GGUF filenames (profiles.json "allowed_models")
  * a NODE reports its loaded model via /props.model_path (a full path)
  * llama-server reports /v1/models ids that may be a path or an alias

So the canonical key is the file BASENAME, case-insensitive, with a trailing .gguf
removed. Display names keep the original spelling. No fuzzy matching, no substrings -
the same rule the old folder-based auth used (exact, case-insensitive only).
"""

import os


def model_basename(value: str) -> str:
    """Return the display name for a model identifier/path ('D:/x/Qwen.gguf' -> 'Qwen.gguf')."""
    if not value:
        return ""
    # Accept Windows or POSIX separators regardless of the current OS.
    cleaned = str(value).strip().replace("\\", "/")
    base = os.path.basename(cleaned)
    return base.strip()


def model_key(value: str) -> str:
    """Return the canonical routing key: lowercase basename, no trailing .gguf."""
    base = model_basename(value).lower()
    if base.endswith(".gguf"):
        base = base[: -len(".gguf")]
    return base


def keys_match(a: str, b: str) -> bool:
    """True when two identifiers/paths refer to the same model (canonical comparison)."""
    ka = model_key(a)
    kb = model_key(b)
    return bool(ka) and ka == kb
