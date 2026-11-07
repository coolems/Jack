"""Live cycle-history recorder for one agentic turn (client side).

`TurnLog` holds the rows; this class is the wire side: it emits one frame per event
so the UI can build the collapsed turn block while the turn runs, and hands the SAME
payload to the persistence path so the DB row and the final frame always agree.

Frame types (all carry `conv_id`):
    cycle_start            turn began (last_turn_sec = previous turn's active time)
    cycle_step             one reasoning step settled (token/speed counters)
    cycle_tool             one tool call finished (ok flag + clipped output)
    cycle_consent_pending  a consent card appeared on screen
    cycle_consent          a consent card was answered ("pending": True tells the UI
                           to REMOVE the card - the card itself is transient)
    cycle_grant            a per-kind "Allow until task done" unlock (once per kind)
    cycle_steer            a steering message was injected (at_ts = when the user typed it)
    cycle_feedback         a loop notice (deduped, never the removed steer notice)
    cycle_final            the answer row opened (step_no of the final answer)
    cycle_end              the settled payload (identical to the persisted row)

Every send is best-effort: a dead socket must never break the loop, so all sends go
through `_send()` which swallows any exception. Recording is likewise best-effort -
ToolExecutor wraps every recorder call, and this class never raises.
"""

import json
import logging
import time
from typing import Any, Dict, Optional

from app.turn_log import TurnLog

logger = logging.getLogger("COOLEMS.CycleRecorder")


class CycleRecorder:
    """One turn's recorder: rows in `self.log`, frames on `self.ws`."""

    def __init__(self, websocket: Any, conv_id: str,
                 turn_started_at: Optional[float] = None, last_turn_sec: float = 0.0):
        self.ws = websocket
        self.conv_id = conv_id
        self.log = TurnLog(turn_started_at=turn_started_at, last_turn_sec=last_turn_sec)
        self.settled = False
        self._step_no = 0
        self._granted_kinds: set = set()
        self._feedback_seen: set = set()
        self._final_announced = False
        self._payload: Optional[Dict] = None

    # --- wire -------------------------------------------------------------

    async def _send(self, frame: Dict) -> None:
        """Send one frame, never raising (UI gone / socket closed is normal here)."""
        if self.ws is None:
            return
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
            "conv_id": self.conv_id,
            "turn_started_at": self.log.turn_started_at,
            "last_turn_sec": round(float(self.log.last_turn_sec or 0.0), 1),
        })

    def begin_step(self) -> int:
        """Start a new reasoning step; returns its step_no."""
        self._step_no += 1
        return self._step_no

    async def settle_step(self, stats: Any, elapsed_sec: float) -> None:
        """Record + announce one settled reasoning step."""
        row = self.log.add_step(self._step_no, stats, elapsed_sec)
        await self._send({"type": "cycle_step", "conv_id": self.conv_id,
                          "step_no": self._step_no, "step": row})

    # --- tools ------------------------------------------------------------

    async def tool(self, name: str, args: Any, ok: bool, output: Any,
                   duration_ms: float) -> None:
        """Record + announce one tool call (also for calls that never ran)."""
        row = self.log.add_tool(self._step_no, name, args, ok, output, duration_ms)
        await self._send({"type": "cycle_tool", "conv_id": self.conv_id,
                          "name": row["name"], "ok": row["ok"],
                          "step_no": row["step_no"], "at_ts": row["at_ts"],
                          "tool": row})

    # --- consent dialogs --------------------------------------------------

    async def consent_pending(self, kind: str, request_id: str, payload: Any = "",
                              description: str = "") -> None:
        """A card is on screen. The later decision folds into THIS row."""
        row = self.log.add_consent_pending(self._step_no, kind, request_id, payload, description)
        await self._send({
            "type": "cycle_consent_pending",
            "conv_id": self.conv_id,
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
            "conv_id": self.conv_id,
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
        await self._send({
            "type": "cycle_grant",
            "conv_id": self.conv_id,
            "kind": kind,
            "request_id": row["request_id"],
            "at_ts": row["at_ts"],
        })

    # --- steers / notices -------------------------------------------------

    async def steer(self, text: str, seq: int = 0, at_ts: Optional[float] = None) -> None:
        """A steering message was injected. `at_ts` is WHEN THE USER TYPED it, not when
        the loop injected it - the UI sorts rows inside a step group by that time."""
        row = self.log.add_steer(self._step_no, text, seq, at_ts)
        await self._send({
            "type": "cycle_steer",
            "conv_id": self.conv_id,
            "text": row["text"],
            "seq": row["seq"],
            "step_no": row["step_no"],
            "at_ts": row["at_ts"],
        })

    async def feedback(self, text: str) -> None:
        """A loop notice. Identical texts are recorded/sent once."""
        if not text:
            return
        if text in self._feedback_seen:
            return
        self._feedback_seen.add(text)
        row = self.log.add_feedback(self._step_no, text)
        await self._send({"type": "cycle_feedback", "conv_id": self.conv_id,
                          "text": row["text"], "step_no": row["step_no"], "at_ts": row["at_ts"]})

    async def announce_final(self, step_no: int) -> None:
        """Open the answer row. Once per turn - a reply to a steer and the real answer
        are identical content frames, so the UI needs this signal to tell them apart."""
        if self._final_announced:
            return
        self._final_announced = True
        await self._send({"type": "cycle_final", "conv_id": self.conv_id, "step_no": int(step_no)})

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
        self.log.set_final(text or "", stopped=bool(stopped))
        self._payload = self.log.payload(float(elapsed_sec or 0.0))
        self.settled = True
        return self._payload

    async def end(self, elapsed_sec: float, text: str, stopped: bool = False) -> Dict:
        """Close the turn: settle (idempotent) and emit the payload as the final frame."""
        payload = self.settle_now(text, stopped=stopped, elapsed_sec=elapsed_sec)
        await self._send({"type": "cycle_end", "conv_id": self.conv_id, "payload": payload})
        return payload

    def payload(self, elapsed_sec: float) -> Dict:
        """The current payload without settling (used by tests / diagnostics)."""
        return self.log.payload(float(elapsed_sec or 0.0))
