"""Live cycle-history recorder for one agentic turn (client side).

`TurnLog` holds the rows; this class is the wire side: it emits one frame per event
so the UI can build the collapsed turn block while the turn runs, and hands the SAME
payload to the persistence path so the DB row and the final frame always agree.

Frame types (all carry `conv_id` AND `turn_id`):
    cycle_start            turn began (last_turn_sec = previous turn's active time)
    cycle_step             one reasoning step settled (token/speed counters)
    cycle_speech           one AI text block of a step (thinking | answer)
    cycle_tool             one tool call finished (ok flag + clipped input/output)
    cycle_consent_pending  a consent card appeared on screen
    cycle_consent          a consent card was answered ("pending": True tells the UI
                           to REMOVE the card - the card itself is transient)
    cycle_grant            a per-kind "Allow until task done" unlock (once per kind)
    cycle_steer            a steering message was injected (at_ts = when the user typed it)
    cycle_feedback         a loop notice (deduped, never the removed steer notice)
    cycle_final            the answer row opened (step_no of the final answer)
    cycle_end              the settled payload (identical to the persisted row)

THE IDENTITY RULE: `turn_id` is minted by the chat bus at turn start and travels on
every frame. The UI refuses to render a frame whose (conv_id, turn_id) pair is not the
block it owns, so frames of one workspace can never land in another workspace's view.

Every send is best-effort: a dead socket must never break the loop, so all sends go
through `_send()` which swallows any exception. Recording is likewise best-effort -
ToolExecutor wraps every recorder call, and this class never raises.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from app.turn_log import TurnLog

logger = logging.getLogger("COOLEMS.CycleRecorder")


class CycleRecorder:
    """One turn's recorder: rows in `self.log`, frames on `self.ws`."""

    def __init__(self, websocket: Any, conv_id: str, turn_id: str = "",
                 turn_started_at: Optional[float] = None, last_turn_sec: float = 0.0):
        self.ws = websocket
        self.conv_id = conv_id
        self.turn_id = turn_id or ""
        self.log = TurnLog(turn_started_at=turn_started_at, last_turn_sec=last_turn_sec,
                           turn_id=self.turn_id)
        self.settled = False
        self._step_no = 0
        self._granted_kinds: set = set()
        self._feedback_seen: set = set()
        self._final_announced = False
        self._payload: Optional[Dict] = None
        # AI text captured for the CURRENT step, flushed when the step advances or the
        # answer row opens - so text never leaks into the next step's group.
        self._speech_buf: List[tuple] = []

    # --- wire -------------------------------------------------------------

    async def _send(self, frame: Dict) -> None:
        """Send one frame, never raising (UI gone / socket closed is normal here)."""
        if self.ws is None:
            return
        frame.setdefault("conv_id", self.conv_id)
        frame.setdefault("turn_id", self.turn_id)
        try:
            await self.ws.send_text(json.dumps(frame))
        except Exception:
            pass

    # --- turn / steps -----------------------------------------------------

    async def start(self) -> None:
        """Announce the turn and remember when it began (elapsed = wall clock later)."""
        self.log.turn_started_at = self.log.turn_started_at if self.log.turn_started_at is not None else time.time()
        await self._send({
            "type": "cycle_start",
            "turn_started_at": self.log.turn_started_at,
            "last_turn_sec": round(float(self.log.last_turn_sec or 0.0), 1),
        })

    async def begin_step(self) -> int:
        """Start a new reasoning step: closes the PREVIOUS step's speech first, so a
        step's AI text never lands in the next step's group. Returns the new step_no."""
        await self._flush_speech()
        self._step_no += 1
        return self._step_no

    def begin_step_sync(self) -> int:
        """Same as begin_step() without the frame sends (sync call sites)."""
        self._flush_speech_rows()
        self._step_no += 1
        return self._step_no

    async def settle_step(self, stats: Any, elapsed_sec: float) -> None:
        """Record + announce one settled reasoning step."""
        row = self.log.add_step(self._step_no, stats, elapsed_sec)
        await self._send({"type": "cycle_step", "step_no": self._step_no, "step": row})

    # --- AI text ----------------------------------------------------------

    def capture(self, text: str, kind: str = "answer") -> None:
        """Buffer one streamed AI chunk of the current step (thinking | answer).

        Chunks are buffered RAW (a marker can be split across them) and scrubbed once at
        flush time, when the whole block of text is assembled - scrubbing per chunk would
        let half a marker through.
        """
        if not text:
            return
        self._speech_buf.append((self._step_no, text, kind))

    def _flush_speech_rows(self) -> List[Dict]:
        """Turn the buffered chunks into rows (sync). Empty buffer = no rows.

        Chunks of the SAME (step, kind) are joined into one row before scrubbing, so a
        split protocol marker is recognised and the strip gets one coherent text block
        instead of a row per socket flush.
        """
        if not self._speech_buf:
            return []
        rows = []
        joined: List[tuple] = []
        for step_no, text, kind in self._speech_buf:
            if joined and joined[-1][0] == step_no and joined[-1][1] == kind:
                joined[-1][2] += text
            else:
                joined.append([step_no, kind, text])
        for step_no, kind, text in joined:
            row = self.log.add_speech(step_no, text, kind)
            if row is not None:
                rows.append(row)
        self._speech_buf = []
        return rows

    async def _flush_speech(self) -> None:
        """Flush buffered AI text into rows AND announce each as a frame."""
        for row in self._flush_speech_rows():
            await self._send({"type": "cycle_speech", "step_no": row["step_no"],
                              "kind": row["kind"], "text": row["text"],
                              "final": row["final"], "at_ts": row["at_ts"]})

    # --- tools ------------------------------------------------------------

    async def tool(self, name: str, args: Any, ok: bool, output: Any,
                   duration_ms: float, call_id: str = "") -> None:
        """Record + announce one tool call (also for calls that never ran)."""
        row = self.log.add_tool(self._step_no, name, args, ok, output, duration_ms,
                                call_id=call_id)
        await self._send({"type": "cycle_tool", "name": row["name"], "ok": row["ok"],
                          "step_no": row["step_no"], "at_ts": row["at_ts"],
                          "call_id": row["call_id"], "duration_ms": row["duration_ms"],
                          "tool": row})

    # --- consent dialogs --------------------------------------------------

    async def consent_pending(self, kind: str, request_id: str, payload: Any = "",
                              description: str = "") -> None:
        """A card is on screen. The later decision folds into THIS row."""
        row = self.log.add_consent_pending(self._step_no, kind, request_id, payload, description)
        await self._send({
            "type": "cycle_consent_pending",
            "kind": kind,
            "request_id": request_id or "",
            "code": "" if payload is None else str(payload),
            "description": description or "",
            "at_ts": row["at_ts"],
        })

    async def consent(self, kind: str, decision: str, request_id: str = "",
                      payload: Any = "") -> None:
        """A card was answered (or could not be answered). `pending: True` = remove the card."""
        row = self.log.add_consent(self._step_no, kind, decision, request_id, payload)
        await self._send({
            "type": "cycle_consent",
            "kind": kind,
            "decision": decision,
            "request_id": request_id or "",
            "pending": True,
            "at_ts": row["at_ts"],
        })

    async def grant(self, kind: str, request_id: str = "") -> None:
        """A per-kind auto-allow unlock. Announced ONCE per kind - a repeat must not
        re-print the unlock line."""
        row = self.log.add_grant(self._step_no, kind, request_id)
        if kind in self._granted_kinds:
            return
        self._granted_kinds.add(kind)
        await self._send({"type": "cycle_grant", "kind": kind,
                          "request_id": row["request_id"], "at_ts": row["at_ts"]})

    # --- steers / notices -------------------------------------------------

    async def steer(self, text: str, seq: int = 0, at_ts: Optional[float] = None,
                    steer_id: str = "") -> None:
        """A steering message was injected. `at_ts` is WHEN THE USER TYPED it, not when
        the loop injected it - the UI places the row in the block at that moment.
        `steer_id` correlates this row with the steer_received/steer_applied ack frames,
        so one steer is ONE row whose status line settles instead of duplicating."""
        row = self.log.add_steer(self._step_no, text, seq, at_ts, steer_id=str(steer_id or ""))
        await self._send({"type": "cycle_steer", "conv_id": self.conv_id,
                          "text": row["text"], "seq": row["seq"],
                          "steer_id": row["steer_id"], "applied": True,
                          "step_no": row["step_no"], "at_ts": row["at_ts"]})

    async def feedback(self, text: str) -> None:
        """A loop notice. Identical texts are recorded/sent once."""
        if not text:
            return
        if text in self._feedback_seen:
            return
        self._feedback_seen.add(text)
        row = self.log.add_feedback(self._step_no, text)
        await self._send({"type": "cycle_feedback", "text": row["text"],
                          "step_no": row["step_no"], "at_ts": row["at_ts"]})

    async def announce_final(self, step_no: int) -> None:
        """Open the answer row. Once per turn - a reply to a steer and the real answer
        are identical content frames, so the UI needs this signal to tell them apart.
        The step's AI text is flushed FIRST so the answer text arrives before the marker."""
        if self._final_announced:
            return
        self._final_announced = True
        self.log.final_step = int(step_no)
        # Re-tag the step's answer rows so the UI can mark the delivered answer.
        for row in self.log.speech:
            if row["kind"] == "answer" and int(row["step_no"]) == int(step_no):
                row["final"] = True
        await self._flush_speech()
        await self._send({"type": "cycle_final", "step_no": int(step_no)})

    # --- settle / end -----------------------------------------------------

    def settle_now(self, text: str, stopped: bool = False,
                   elapsed_sec: Optional[float] = None) -> Dict:
        """Settle the turn SYNCHRONOUSLY and idempotently.

        A turn stuck inside a consent card is HARD-cancelled by the bus backstop, so the
        graceful settle never runs - this is the path that still folds the card as
        'unanswered'. Every later caller (persist + the cycle_end frame) gets the SAME
        payload object, so nobody re-settles or changes the numbers.
        """
        if self._payload is not None:
            return self._payload
        if elapsed_sec is None:
            elapsed_sec = max(0.0, time.time() - float(self.log.turn_started_at or time.time()))
        self._flush_speech_rows()   # partial words must survive a hard cancel
        self.log.set_final(text or "", stopped=bool(stopped))
        self._payload = self.log.payload(float(elapsed_sec or 0.0))
        self.settled = True
        return self._payload

    async def end(self, elapsed_sec: float, text: str, stopped: bool = False) -> Dict:
        """Close the turn: settle (idempotent) and emit the payload as the final frame."""
        payload = self.settle_now(text, stopped=stopped, elapsed_sec=elapsed_sec)
        await self._send({"type": "cycle_end", "payload": payload})
        return payload

    def payload(self, elapsed_sec: float) -> Dict:
        """The current payload without settling (used by tests / diagnostics)."""
        return self.log.payload(float(elapsed_sec or 0.0))
