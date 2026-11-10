"""OutputRing — one bounded byte stream with offsets that NEVER move (client side).

A background job's output is read by two consumers that must not disturb each other:

  * the MODEL consumes through a registry-kept cursor (each read advances it), so a
    tool result contains exactly the bytes it has not seen yet;
  * UI observers read at ABSOLUTE offsets (`read_at`), so live output can be painted
    without stealing the model's bytes.

Chunk boundaries are the unit of retention: appends land whole, trimming only drops
whole chunks from the head, so a consumer concatenating chunks under `next` can never
see a duplicated or half-cut chunk. When a requested offset slid below the retained
window the read is `lossy` (bytes are missing before the chunks) — never an error.

A spill file keeps the COMPLETE stream, so a reader below the evicted head still gets a
path to the full text instead of a silent truncation.

Pure data structure: no asyncio, no I/O assumptions beyond an optional spill path.
"""

from typing import Callable, Dict, List, Optional, Tuple

#: Stream labels. 'log' is narration for observers only — the model never reads it.
CHANNELS = ("stdout", "stderr", "log")

#: Rendered once per lossy read, so a reader never splices text whose front is missing.
_GAP_LINE = "[... output lost: earlier bytes evicted from the ring ...]"


class OutputRing:
    """Bounded append-only text stream with absolute byte offsets."""

    def __init__(self, retain_bytes: int, spill_path: Optional[str] = None,
                 spill_writer: Optional[Callable[[str], None]] = None):
        self._retain = max(1, int(retain_bytes))
        self._spill_path = spill_path
        self._spill_writer = spill_writer
        self._chunks: List[Dict] = []      # {at, bytes, channel, gap_before}
        self._total = 0                     # absolute offset of the stream end
        self._retained_start = 0            # absolute offset of the oldest retained byte
        self._dropped_bytes = 0
        self._closed = False

    # --- producer side -----------------------------------------------------

    def append(self, text: str, channel: str = "stdout", gap_before: bool = False) -> int:
        """Append one chunk. Returns its absolute start offset (0 for an empty chunk).

        An empty chunk is dropped without touching offsets, so a producer flushing a
        trailing newline never wakes a reader with nothing to read.
        """
        if self._closed or not text:
            return self._total
        payload = text.encode("utf-8", "replace")
        at = self._total
        self._chunks.append({"at": at, "bytes": payload,
                             "channel": channel if channel in CHANNELS else "stdout",
                             "gap_before": bool(gap_before)})
        self._total += len(payload)
        if self._spill_writer is not None:
            try:
                self._spill_writer(text)
            except Exception:
                pass  # a broken spill must never break the producer
        self._trim()
        return at

    def _trim(self) -> None:
        """Drop whole chunks from the head until the retained window fits."""
        kept = sum(len(c["bytes"]) for c in self._chunks)
        while kept > self._retain and self._chunks:
            dropped = self._chunks.pop(0)
            kept -= len(dropped["bytes"])
            self._retained_start = dropped["at"] + len(dropped["bytes"])
            self._dropped_bytes += len(dropped["bytes"])
        # Eviction is reported by the READ, not by a chunk marker: `read_at()` returns
        # lossy=True when the requested offset slid below the retained window. Marking the
        # oldest retained chunk would claim lost bytes for a read that IS complete (a read
        # at _retained_start sees everything still held), which is a lie about the stream.
        # A producer-reported discontinuity (a pull source whose own file slid) is marked
        # explicitly on its chunk via gap_before, and that marker is always rendered.

    def read_text(self, from_offset: int, include_log: bool = False) -> Tuple[str, bool, int]:
        """(text, lossy, next) in one call, so a caller cannot print chunks without the gap.

        The gap line is rendered when THIS read is lossy (bytes before it were evicted),
        and never for a complete read.
        """
        chunks, lossy, nxt = self.read_at(from_offset)
        parts = []
        if lossy:
            parts.append(_GAP_LINE)
        for c in chunks:
            if c["channel"] == "log" and not include_log:
                continue
            if c["gap_before"]:
                parts.append(_GAP_LINE)
            parts.append(c["bytes"].decode("utf-8", "replace"))
        return "".join(parts), lossy, nxt

    def shrink(self, retain_bytes: int) -> None:
        """Settle-time retention: the terminal notice already carries what fits."""
        self._retain = max(1, int(retain_bytes))
        self._trim()

    def close(self) -> None:
        """End the stream. Late writes drop instead of throwing (a producer's trailing
        flush must never break its own teardown path)."""
        self._closed = True

    # --- consumer side -----------------------------------------------------

    def read_at(self, from_offset: int) -> Tuple[List[Dict], bool, int]:
        """Read retained chunks overlapping [from_offset, total) without consuming.

        Returns (chunks, lossy, next). `next` is always a chunk boundary (the current
        total), which is what a caller resumes from. A chunk whose start precedes
        `from_offset` is returned whole — partial chunks would duplicate text.
        """
        from_offset = max(0, int(from_offset))
        lossy = from_offset < self._retained_start
        out = [c for c in self._chunks if c["at"] + len(c["bytes"]) > from_offset]
        return out, lossy, self._total

    def read(self, cursor: int) -> Tuple[List[Dict], bool, int]:
        """Consume from the model cursor. Returns (chunks, lossy, next_cursor)."""
        return self.read_at(cursor)

    def text(self, chunks: List[Dict], include_log: bool = False) -> str:
        """Render chunks as text, skipping the observers-only 'log' channel."""
        parts = []
        for c in chunks:
            if c["channel"] == "log" and not include_log:
                continue
            if c["gap_before"]:
                parts.append("[... output lost: earlier bytes evicted from the ring ...]")
            parts.append(c["bytes"].decode("utf-8", "replace"))
        return "".join(parts)

    # --- projection --------------------------------------------------------

    def view(self) -> Dict:
        """Observer-safe projection (no chunk payloads)."""
        return {
            "total_bytes": self._total,
            "retained_from": self._retained_start,
            "dropped_bytes": self._dropped_bytes,
            "chunks": len(self._chunks),
            "closed": self._closed,
            "spill_path": self._spill_path or "",
        }

    @property
    def total(self) -> int:
        return self._total

    @property
    def retained_start(self) -> int:
        return self._retained_start

    @property
    def spill_path(self) -> Optional[str]:
        return self._spill_path
