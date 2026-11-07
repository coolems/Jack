"""What a mid-run steering message MEANS (client side, 2026-11).

A message typed while a task is running is an INTERJECTION by default: the user wants an
answer / a correction and the job must keep going. Only an EXPLICIT stop request may close
the task early. The loop injects one of the two headers below so the model cannot guess
which one it got - that distinction is what makes "tell me the current time" mid-review
continue the review instead of ending it.

This module owns ONLY the classification + the header text. react_loop.py applies the
result (injection, deferral of a premature completion, the resume notice).
"""

import re
from typing import List, Tuple

#: Injected before an interjection: answer it, then keep working.
STEER_INTERJECTION_HEADER = (
    "[USER MESSAGE WHILE YOU WERE WORKING - this is NOT a stop request. "
    "Answer or consider it now, then CONTINUE the original task from where you left it. "
    "Do NOT end the task, do NOT restart it from scratch, and do NOT write the completion "
    "marker just because you answered this message. End with 'Agentic AI: TASK DONE' only "
    "when the ORIGINAL task (as now specified) is genuinely finished.]"
)

#: Injected before an explicit stop: the user asked to end the work.
STEER_STOP_HEADER = (
    "[USER STOP REQUEST - the user explicitly asked to STOP the current task. "
    "STOP the work now: do not call further tools, summarize what you completed and what "
    "remains, and end your response with 'Agentic AI: TASK DONE'.]"
)

#: Verbs that mean "end this run" when they are not negated.
_STOP_VERBS = re.compile(
    r"\b(stop|stopping|halt|halting|cancel|cancelling|canceling|abort|aborting|"
    r"terminate|terminating|quit|interrupt|interrupting|pause|pausing)\b",
    re.IGNORECASE,
)

#: Multi-word stop phrases that a bare verb would miss.
_STOP_PHRASES = (
    "end the task", "end this task", "end the run", "end the loop", "end it now",
    "just answer", "answer only", "no more tools", "no more tool calls",
    "that's all", "thats all", "forget it", "leave it as is", "skip the rest",
)

#: Words in the ~14 chars BEFORE a stop verb that negate it ("don't stop", "without stopping").
_NEGATIONS = ("don't", "dont", "do not", "doesn't", "doesnt", "not", "never",
              "no need", "without", "avoid", "can't", "cannot", "please keep")

_NEGATED_WINDOW = 14


def is_explicit_stop(text: str) -> bool:
    """True only when the user explicitly asked to stop / cancel / end the task.

    Negated forms are NOT stops: "don't stop, keep going", "without stopping, add the
    tests". Questions and corrections are NOT stops either - they carry no stop verb.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    lowered = text.lower()

    for phrase in _STOP_PHRASES:
        if phrase in lowered:
            return True

    for match in _STOP_VERBS.finditer(lowered):
        window = lowered[max(0, match.start() - _NEGATED_WINDOW):match.start()]
        if any(neg in window for neg in _NEGATIONS):
            continue   # "don't stop" / "without stopping" - the user wants the job to continue
        return True

    return False


def steer_header_for(text: str) -> str:
    """The header that must precede this steering message."""
    return STEER_STOP_HEADER if is_explicit_stop(text) else STEER_INTERJECTION_HEADER


def classify_steers(texts: List[str]) -> Tuple[bool, bool]:
    """(has_interjection, has_stop) for one drained batch.

    A batch holding BOTH still reports both: the loop only pushes a premature completion
    back when the batch was pure interjection, so a stop request is honoured.
    """
    interjection = False
    stop = False
    for text in texts or []:
        if is_explicit_stop(text):
            stop = True
        else:
            interjection = True
    return interjection, stop


class SteerOutcome(int):
    """How many steers were injected, plus what they meant.

    Subclasses int so every existing `if n:` / `n == 2` call site keeps working while the
    loop can also ask `outcome.interjection` / `outcome.stop`.
    """

    def __new__(cls, count: int, interjection: bool = False, stop: bool = False):
        obj = super().__new__(cls, int(count or 0))
        obj.interjection = bool(interjection)
        obj.stop = bool(stop)
        return obj


def interjection_resume_notice(applied: int, total: int) -> str:
    """The notice the loop sends when it deferred a premature completion.

    It names the ratio (answered vs asked) so the user sees the job stayed open, and
    tells the model plainly not to close the task.
    """
    return (
        f"Answered {int(applied)}/{int(total)} mid-run message(s) - that was not a stop "
        "request, so the task stayed open. Continue the original task now; Do NOT stop "
        "or write the completion marker until the original task is finished."
    )
