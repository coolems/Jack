"""Cycle-history record for one agentic turn (client side).

A "cycle" is one turn: every reasoning step, tool call, consent dialog, steer and
notice that happened while the turn ran, plus every AI text and the final answer. The
record is sent live as frames and persisted as one row in the `turn_log` table, so a
workspace reload rebuilds the collapsed turn block exactly as it looked on screen.

THE IDENTITY CONTRACT (why this exists): a `turn_id` is minted by the chat bus when the
turn starts and is carried on EVERY frame and inside the payload. The UI keys its blocks
by (conv_id, turn_id) and refuses a turn_id that another conversation already owns, so a
frame of workspace A can never land in workspace B's block - on a live push, a background
replay or a DB reload.

Why a plain dict payload: the row is stored as JSON text and handed straight to the UI.
No dataclasses, no schema drift - `parse_payload()` is the only reader.

NOTE (workspace deletion): `turn_log.conversation_id` is a CHILD of
`conversations.id` (foreign key) and app/db_manager enforces foreign keys on every
connection, so a workspace delete must clear these rows FIRST. See
app/websocket/turn_ops.delete_conversation_rows().
"""

import json
import re
import time
from typing import Any, Dict, List, Optional

# ===== DISPLAY SCRUB (2026-11-08, user request: "the AI response block has lines I do
# not need there at all") =====
#
# The AI strip must show ONLY what the AI said to the user. These lines are protocol or
# bookkeeping, not speech, and they were landing inside the answer:
#   * 'Loop stats: N / M | ...'    - the spliced turn-stats line (already on the block head),
#   * '<tool_call>...</tool_call>'  - llama.cpp streams tool-call markup as content when the
#                                     model emits it as text; the tool row already shows the call,
#   * '[SYSTEM NOTICE] ...' / the user-message headers - loop plumbing, shown as block rows.
#
# EXACT-SHAPE matching only (same discipline as logic/base_mode._TURN_STATS_LINE_RE), so prose
# that merely mentions one of these words survives.
#
# THE COMPLETION MARKER IS NOT SCRUBBED (restored 2026-11-08, user request: "i want to have
# this as it was before - i do not like code that hides it"). 'Agentic AI: TASK DONE' is the
# loop's stop signal (logic/react_loop.py reads it from the provider text), so the user must be
# able to SEE it in the answer: it proves why the turn ended. The scrub never touches the model
# text or the persisted final answer - it only removes markup/notice lines from speech rows.
# static/ui/cycle-rows.js mirrors these patterns.
_AI_NOISE_PATTERNS = (
    re.compile(r"(?m)^[ \t]*Loop stats:\s*\d+\s*/\s*\d+[^\n]*$"),
    re.compile(r"(?m)^[ \t]*<tool_call>[\s\S]*?</tool_call>[ \t]*$"),
    re.compile(r"(?m)^[ \t]*<tool_call>[^\n]*$"),
    re.compile(r"(?m)^[ \t]*<function[^\n]*>$"),
    re.compile(r"(?m)^[ \t]*</function>[ \t]*$"),
    re.compile(r"(?m)^[ \t]*\[SYSTEM NOTICE\][^\n]*$"),
    re.compile(r"(?m)^[ \t]*\[USER MESSAGE WHILE YOU WERE WORKING[^\n]*$"),
    re.compile(r"(?m)^[ \t]*\[USER STOP REQUEST[^\n]*$"),
)


def scrub_ai_text(text: str) -> str:
    """Remove protocol/bookkeeping lines from AI speech. Returns "" when nothing is left."""
    if not isinstance(text, str) or not text:
        return ""
    cleaned = text
    for pat in _AI_NOISE_PATTERNS:
        cleaned = pat.sub("", cleaned)
    if cleaned == text:
        return cleaned
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip("\n")

# Tool input/output budgets for the persisted turn row. The always-visible LAST-TOOL strip
# shows the FULL text (no line cap), so these budgets are what the user can actually read;
# only the expanded body rows are clipped to 5 input / 4 output lines by the UI.
# 600/1200 used to hide genuine results - the middle of a long output is still marked.
TOOL_INPUT_KEEP = 2000
TOOL_OUTPUT_KEEP = 8000

# Marker for a consent card that is on screen but not answered yet.
PENDING_DECISION = "pending"

# Decision recorded when the turn ends while a card is still unanswered.
UNANSWERED_DECISION = "unanswered"

#: Legacy wording: a "Allow until task done" unlock used to be recorded as a message
#: line in `feedback`. Rows written by that build are normalized into a `grants` row on
#: read, so the cycle block shows one unlock row instead of an AI-speech-looking line.
LEGACY_GRANT_FEEDBACK_PREFIX = "Allow-until-task-done enabled:"


def _dedupe_feedback(rows: List[Dict]) -> List[Dict]:
    """Drop repeated notice texts inside the SAME step (the loop can re-emit one notice)."""
    seen: set = set()
    out: List[Dict] = []
    for row in rows:
        key = (row.get("step_no"), row.get("text"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _normalize_legacy_grant_lines(data: Dict) -> Dict:
    """Turn an old grant MESSAGE line into a `grants` row (once per consent row).

    Only payloads that have NO grant rows are rebuilt - a payload that already carries
    them keeps them untouched, so a new row is never duplicated or reordered.
    """
    consents = data.get("consents") or []
    feedback = data.get("feedback") or []

    if not data.get("grants"):
        grant_rows: List[Dict] = []
        grant_steps: set = set()
        for c in consents:
            if c.get("decision") != "allow_category_until_done":
                continue
            step_no = int(c.get("step_no", 0) or 0)
            if step_no in grant_steps:
                continue
            grant_steps.add(step_no)
            grant_rows.append({"step_no": step_no, "kind": c.get("kind", "python"),
                               "request_id": c.get("request_id", "")})
        if grant_rows:
            data["grants"] = grant_rows
            # The matching message line is the same fact - drop it from the notices.
            feedback = [f for f in feedback
                        if not str(f.get("text", "")).startswith(LEGACY_GRANT_FEEDBACK_PREFIX)]

    data["feedback"] = _dedupe_feedback(feedback)
    if not data.get("speech"):
        data["speech"] = []
    return data


def parse_payload(raw: Any) -> Optional[Dict]:
    """Parse a stored payload. Never raises - bad/empty data means 'no history'.

    Legacy rows (grant message lines, repeated notices, missing speech) are normalized
    here, so the UI reader sees the same shape for old rows and new ones.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data = _normalize_legacy_grant_lines(data)
    data["counts"] = counts_of(data)
    return data


def counts_of(data: Dict) -> Dict[str, int]:
    """The summary-line counters, derived from the rows (single source of truth)."""
    tools = data.get("tools") or []
    return {
        "turns": len(data.get("steps") or []),
        "tools": len(tools),
        "tools_ok": sum(1 for t in tools if t.get("ok")),
        "consents": len(data.get("consents") or []),
        "grants": len(data.get("grants") or []),
        "steers": len(data.get("steers") or []),
        "notices": len(data.get("feedback") or []),
    }


def _clip(text: str, keep: int) -> str:
    """Keep the head and tail of a long string (the interesting parts)."""
    if not isinstance(text, str) or len(text) <= keep:
        return text
    head = text[: keep // 2]
    tail = text[-(keep // 2):]
    clipped = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{clipped} chars clipped] ...\n\n{tail}"


class TurnLog:
    """Counters + rows for one turn. Every row carries its own time (at_ts) so the
    UI can interleave steers with tools/consents inside a step group."""

    def __init__(self, turn_started_at: Optional[float] = None, last_turn_sec: float = 0.0,
                 turn_id: str = ""):
        self.turn_id = turn_id or ""
        self.turn_started_at = turn_started_at if turn_started_at is not None else time.time()
        self.last_turn_sec = float(last_turn_sec or 0.0)
        self.steps: List[Dict] = []
        self.tools: List[Dict] = []
        self.consents: List[Dict] = []
        self.steers: List[Dict] = []
        self.grants: List[Dict] = []
        self.feedback: List[Dict] = []
        self.speech: List[Dict] = []
        self.final: Optional[Dict] = None
        self.final_step: int = 0
        self.turn_generated = 0
        self._elapsed_total = 0.0

    # ===== steps =====

    def add_step(self, step_no: int, stats: Any, elapsed_sec: float) -> Dict:
        """Record one reasoning step. Same step_no twice = a retry path: the later
        call replaces the earlier one instead of duplicating the line."""
        generated = int(getattr(stats, "generated_tokens", 0) or 0)
        speed = float(getattr(stats, "calculated_speed", 0.0) or 0.0)
        self._elapsed_total += float(elapsed_sec or 0.0)

        row = {
            "step_no": int(step_no),
            "generated_tokens": generated,
            "speed_tps": round(speed, 1),
            "duration_sec": round(float(getattr(stats, "total_duration", 0.0) or 0.0), 2),
            "prompt_tokens": int(getattr(stats, "prompt_tokens", 0) or 0),
            "context_window": int(getattr(stats, "context_window", 0) or 0),
            "turn_generated": self.turn_generated + generated,
            "turn_elapsed_sec": round(self._elapsed_total, 1),
            "at_ts": time.time(),
        }

        existing = next((s for s in self.steps if s["step_no"] == row["step_no"]), None)
        if existing is None:
            self.steps.append(row)
            self.turn_generated += generated
        else:
            # Retry: replace, and re-count the delta so the total stays honest.
            self.turn_generated += generated - existing["generated_tokens"]
            row["turn_generated"] = self.turn_generated
            self.steps[self.steps.index(existing)] = row
        return row

    # ===== tools =====

    def add_tool(self, step_no: int, name: str, args: Any, ok: bool,
                 output: Any, duration_ms: float, at_ts: Optional[float] = None,
                 call_id: str = "") -> Dict:
        ts = at_ts if at_ts is not None else time.time()
        if isinstance(args, (dict, list)):
            args_text = json.dumps(args, ensure_ascii=False)
        else:
            args_text = "" if args is None else str(args)
        row = {
            "step_no": int(step_no),
            "call_id": call_id or "",
            "name": name,
            "args": _clip(args_text, TOOL_INPUT_KEEP),
            "ok": bool(ok),
            "output": _clip("" if output is None else str(output), TOOL_OUTPUT_KEEP),
            "duration_ms": round(float(duration_ms or 0.0), 1),
            "started_at": ts,
            "at_ts": ts,
        }
        self.tools.append(row)
        return row

    # ===== consent dialogs =====

    def add_consent_pending(self, step_no: int, kind: str, request_id: str,
                            payload: Any = "", description: str = "") -> Dict:
        """Open a row for a card that is on screen. The decision folds into THIS row."""
        row = {
            "step_no": int(step_no),
            "kind": kind,
            "decision": PENDING_DECISION,
            "request_id": request_id or "",
            "payload": "" if payload is None else str(payload),
            "description": description or "",
            "pending": True,
            "at_ts": time.time(),
        }
        self.consents.append(row)
        return row

    def find_consent(self, request_id: str) -> Optional[Dict]:
        """The still-pending row for this card, or None."""
        return next((c for c in self.consents
                     if c["request_id"] == request_id and c["pending"]), None)

    def add_consent(self, step_no: int, kind: str, decision: str,
                    request_id: str = "", payload: Any = "") -> Dict:
        """Record an answer. If the card was opened first, the SAME row is folded -
        an answered card must never produce two history lines."""
        row = self.find_consent(request_id) if request_id else None
        if row is None:
            row = {
                "step_no": int(step_no),
                "kind": kind,
                "decision": decision,
                "request_id": request_id or "",
                "payload": "" if payload is None else str(payload),
                "description": "",
                "pending": False,
                "at_ts": time.time(),
            }
            self.consents.append(row)
        else:
            row["decision"] = decision
            row["pending"] = False
            if payload is not None and not row["payload"]:
                row["payload"] = str(payload)
        return row

    def add_grant(self, step_no: int, kind: str, request_id: str = "",
                  at_ts: Optional[float] = None) -> Dict:
        """A consent auto-allow timer / category grant that skipped the card.

        ONE row PER KIND: a later auto-approved call of the same kind is the same fact,
        so it must not print a second unlock row. The FIRST row (first request_id) wins.
        """
        existing = next((g for g in self.grants if g["kind"] == kind), None)
        if existing is not None:
            return existing
        row = {"step_no": int(step_no), "kind": kind,
               "request_id": request_id or "",
               "at_ts": at_ts if at_ts is not None else time.time()}
        self.grants.append(row)
        return row

    # ===== steers / notices =====

    def add_steer(self, step_no: int, text: str, seq: int = 0,
                  at_ts: Optional[float] = None) -> Dict:
        """A steering message the loop actually injected."""
        row = {"step_no": int(step_no), "text": text or "", "seq": int(seq or 0),
               "applied": True, "reason": "",
               "at_ts": at_ts if at_ts is not None else time.time()}
        self.steers.append(row)
        return row

    def add_feedback(self, step_no: int, text: str,
                     at_ts: Optional[float] = None) -> Dict:
        """A loop notice. The same text inside the SAME step is recorded once - the loop
        can re-emit one notice (retry path) and the block must not show it twice."""
        existing = next((f for f in self.feedback
                         if f["step_no"] == int(step_no) and f["text"] == (text or "")), None)
        if existing is not None:
            return existing
        row = {"step_no": int(step_no), "text": text or "",
               "at_ts": at_ts if at_ts is not None else time.time()}
        self.feedback.append(row)
        return row

    # ===== AI text =====

    def add_speech(self, step_no: int, text: str, kind: str = "answer") -> Optional[Dict]:
        """One AI text block of one reasoning step. `kind` is 'thinking' (reasoning
        stream) or 'answer' (content stream).

        The text is scrubbed first (see `scrub_ai_text`): protocol lines are facts of the
        loop, not speech, and they must never sit in the AI response strip. A chunk that is
        pure noise records NOTHING, so the strip only ever grows with real words.
        """
        visible = scrub_ai_text(text)
        if not visible:
            return None
        row = {"step_no": int(step_no), "kind": kind if kind in ("thinking", "answer") else "answer",
               "text": visible, "at_ts": time.time(),
               "final": bool(kind == "answer" and int(step_no) == self.final_step)}
        self.speech.append(row)
        return row

    def set_final(self, text: str, stopped: bool = False) -> Dict:
        self.final = {"text": text or "", "stopped": bool(stopped)}
        return self.final

    # ===== payload =====

    def payload(self, elapsed_sec: float) -> Dict:
        """The persisted/frames payload. Cards nobody answered are settled here -
        a pending card must not survive into history as 'pending'."""
        for c in self.consents:
            if c["pending"]:
                c["decision"] = UNANSWERED_DECISION
                c["pending"] = False

        speeds = [s["speed_tps"] for s in self.steps if s["speed_tps"]]
        avg_speed = round(sum(speeds) / len(speeds), 1) if speeds else 0.0
        last_step = self.steps[-1] if self.steps else {}

        data = {
            "turn_id": self.turn_id,
            "turn_started_at": self.turn_started_at,
            "elapsed_sec": round(float(elapsed_sec or 0.0), 1),
            "last_turn_sec": round(float(self.last_turn_sec or 0.0), 1),
            "turn_generated": self.turn_generated,
            "window": int(last_step.get("context_window", 0) or 0),
            "prompt_tokens": int(last_step.get("prompt_tokens", 0) or 0),
            "speed_tps": avg_speed,
            "steps": self.steps,
            "tools": self.tools,
            "speech": self.speech,
            "steers": self.steers,
            "consents": self.consents,
            "grants": self.grants,
            "feedback": self.feedback,
            "final": self.final or {"text": "", "stopped": False},
        }
        data["counts"] = counts_of(data)
        return data
