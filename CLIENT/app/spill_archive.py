"""spill_archive — keep a bounded number of scratch payload files (2026-12).

Background jobs and MCP servers write complete streams/payloads into .temp so a reader
below the evicted ring head still gets a locator. Those files accumulate forever otherwise:
a long-running CLIENT would fill .temp with logs nobody can read anymore.

Rule: keep the NEWEST *keep* files in one directory, delete the rest. Only files matching
the given suffix are touched, so a shared scratch directory never loses anything else.
Called at startup (bounded, best-effort) — never on a hot path.
"""

import logging
import os
from typing import List

logger = logging.getLogger("COOLEMS.SpillArchive")


def prune_spill_dir(directory: str, *, keep: int, suffix: str = ".log") -> List[str]:
    """Delete the oldest *suffix* files in *directory* beyond the newest *keep*.

    Returns the deleted paths. Never raises: a failed cleanup must not break startup, and
    a missing directory is simply nothing to do.
    """
    if not directory or keep <= 0 or not suffix:
        return []
    try:
        entries = [os.path.join(directory, name) for name in os.listdir(directory)
                   if name.endswith(suffix)]
    except OSError:
        return []
    if len(entries) <= keep:
        return []
    try:
        entries.sort(key=lambda p: os.stat(p).st_mtime)
    except OSError:
        return []
    deleted: List[str] = []
    for path in entries[:-keep]:
        try:
            os.remove(path)
            deleted.append(path)
        except OSError:
            pass  # a locked file is left alone; the next startup retries
    if deleted:
        logger.info("[SPILL] kept %s newest %s file(s) in %s, removed %d older",
                    keep, suffix, directory, len(deleted))
    return deleted
