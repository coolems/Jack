"""Security event reporting - the ONE place rogue client calls are collected.

Every request that CLAIMS something it cannot prove lands here: an unknown workspace id,
another client's workspace, no workspace scope at all, a path that resolves outside the
workspace folder, an attempt to delete the workspace folder itself. An event is written to
three places so an admin can review it later without reproducing anything:

  1. ``<CLIENT>/logs/security_events.jsonl`` - append-only, one JSON object per line,
     size-capped (the previous file survives as ``.1``). This is the file to hand to a
     reviewer.
  2. the ``COOLEMS.Security`` logger at WARNING - so the same line also shows up in the
     normal ``coolems.log`` stream.
  3. an in-memory ring of the last KEEP_EVENTS events + a per-kind counter - what
     ``GET /api/security/events`` serves.

Recording NEVER raises: a broken log file must not turn a 403 into a 500. Every string is
sanitised before it is stored (caller-controlled, single line, capped), because a raw
newline in a conversation id would otherwise forge log entries.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter, OrderedDict, deque
from datetime import datetime, timezone

logger = logging.getLogger("COOLEMS.Security")

# Anchored to this module (app/ -> <CLIENT>/logs), never to the CWD or the working root:
# security evidence must not land inside a workspace a client can edit.
_LOGS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))

EVENT_FILE_NAME = "security_events.jsonl"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FIELD_CHARS = 200
KEEP_EVENTS = 500

_events: "deque[dict]" = deque(maxlen=KEEP_EVENTS)
_counts: Counter = Counter()
_lock = threading.Lock()

# Identical caller mistakes arrive in bursts: the UI chat-status poll, a file panel that
# retries an image, a background refresh - all hitting the same refusal every couple of
# seconds. Without coalescing they bury the one event an admin needs to see. The same
# (kind, client, target) inside COALESCE_WINDOW_S is counted but written ONCE.
COALESCE_WINDOW_S = 5.0
MAX_COALESCE_KEYS = 256
_last_seen: "OrderedDict[tuple, float]" = OrderedDict()


def event_file() -> str:
    """Absolute path of the JSONL event log."""
    return os.path.join(_LOGS_DIR, EVENT_FILE_NAME)


def _clean(value):
    """Single-line, printable, length-capped copy of a caller-controlled value."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return text[:MAX_FIELD_CHARS]


def _rotate_if_needed(path):
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


def _write(event):
    path = event_file()
    try:
        os.makedirs(_LOGS_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        _rotate_if_needed(path)
    except Exception as exc:  # reporting must never break the request it reports on
        logger.error(f"Could not write security event: {exc}")


def _coalesced(event: dict) -> bool:
    """True when the same event was just recorded; counts it without writing again."""
    key = (
        event["kind"],
        event.get("client_ip", ""),
        event.get("workspace") or event.get("requested") or event.get("claimed") or "",
        event.get("path", ""),
    )
    now = time.monotonic()
    with _lock:
        last = _last_seen.get(key)
        if last is not None and now - last < COALESCE_WINDOW_S:
            _last_seen.move_to_end(key)
            return True
        _last_seen[key] = now
        _last_seen.move_to_end(key)
        while len(_last_seen) > MAX_COALESCE_KEYS:
            _last_seen.popitem(last=False)
    return False


def record(kind: str, **fields) -> dict:
    """Record one security event and return the stored dict. Never raises."""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": _clean(kind) or "unknown",
    }
    for name, value in fields.items():
        cleaned = _clean(value)
        if cleaned not in (None, ""):
            event[_clean(name) or "field"] = cleaned

    detail = " ".join(f"{name}={value!r}" for name, value in event.items() if name not in ("ts", "kind"))
    try:
        with _lock:
            _counts[event["kind"]] += 1
        if _coalesced(event):
            return event
        with _lock:
            _events.append(event)
        _write(event)
        logger.warning(f"SECURITY {event['kind']}: {detail}")
    except Exception as exc:
        logger.error(f"Security event reporting failed: {exc}")
    return event


def recent(limit: int = 50) -> list:
    """Newest-first copy of the in-memory events."""
    with _lock:
        snapshot = list(_events)
    return list(reversed(snapshot[-limit:]))


def summary() -> dict:
    """Per-kind counters since process start."""
    with _lock:
        by_kind = dict(_counts)
    return {"total": sum(by_kind.values()), "by_kind": by_kind}


def clear() -> None:
    """Drop the in-memory ring, counters and coalescing state (tests; the file stays)."""
    with _lock:
        _events.clear()
        _counts.clear()
        _last_seen.clear()
