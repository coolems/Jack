"""Runtime notices — the RUNTIME speaking to the model (client side, 2026-12).

A steering message is USER speech typed while a task runs. A NOTICE is something the
runtime itself learned: a subagent settled, a background job finished, a question the AI
asked was answered. Both are injected at the ReAct loop's safe boundaries, but they are
NOT the same thing and must never be conflated:

  * a steer sets `interjection_pending` (a pure question defers the final answer);
  * a notice NEVER defers completion — the work the notice reports is the work itself.
    A settled notice arriving during a final answer re-opens the turn instead, because
    "all children done" is new information, not a request to stop.
  * a steer is persisted as messages.is_steer (a row of the turn block);
  * a notice is persisted as messages.is_notice, with its header kept in the row so the
    model can still tell WHO said it when a later turn loads history.

Headers are literal text the model sees. They say what happened, what is NOT known yet,
and what to do next — never a summary of the child's result (the payload carries that).

DELIVERY CONTRACT (2026-11-30, checked against deepseek-harness):
  * a BUSY owner is injected - the notice waits in the channel queue and lands at the
    loop's next safe boundary, so N settlements settling together cost ONE step;
  * an IDLE owner is woken - a budgeted follow-up turn (NOTICE_MAX_CONSECUTIVE_WAKES),
    because an undelivered notice is a completion the model never learns about;
  * a settlement the model ALREADY received through a tool result announces nothing
    (jobs.py claim ledger; children.py marks a continuation notified so it is not
    announced as a second delegation);
  * a teardown settlement always announces - nobody is left to receive it otherwise.

ONE deliberate divergence from the harness: when a notice arrives while the FINAL answer
is streaming, we RE-OPEN the turn in the same conversation instead of ending the turn and
waking it later. Same effect (the model acts on the evidence), but it keeps the context,
costs no wake budget, and cannot be lost if the channel is disposed between the answer and
the wake. react_loop applies this; this module owns only the text and the source tags.

Owned by this module: header text + classification. react_loop applies it.
"""

from typing import Dict, List, Tuple

#: Source tags — one per runtime lane. Stored in turn_log rows and the DB flag column.
NOTICE_SUBAGENT = "subagent"
NOTICE_JOB = "job"
NOTICE_QUESTION = "question"
NOTICE_RUNTIME = "runtime"

#: Injected before a settled subagent's report. The child's words are NOT the parent's:
#: the parent must treat them as evidence it received, never as something it wrote.
SUBAGENT_SETTLED_HEADER = (
    "[RUNTIME NOTICE - SUBAGENT SETTLED. This is NOT a user message and NOT a stop "
    "request. A delegated child agent finished; its report follows below. Treat it as "
    "evidence you received - do not present the child's words as your own work, and do "
    "not re-run the same delegation. Read the report, then CONTINUE the original task: "
    "if other delegated children or background jobs are still running, collect them "
    "before your final answer.]"
)

#: Injected before a settled background job's output.
JOB_SETTLED_HEADER = (
    "[RUNTIME NOTICE - BACKGROUND JOB SETTLED. This is NOT a user message and NOT a "
    "stop request. A background job you started finished; its status and unread output "
    "follow below. Use job_output for anything truncated. Keep working on the original "
    "task and collect every still-relevant job before your final text answer.]"
)

#: Injected before the user's answer to a question the AI asked (ask_user_question).
QUESTION_ANSWERED_HEADER = (
    "[RUNTIME NOTICE - THE USER ANSWERED YOUR QUESTION. This is NOT a stop request. "
    "The answer follows below. It is the user's decision: honour it exactly, then "
    "CONTINUE the task from where you left it. A pending answer means the user has not "
    "answered yet - that is not a refusal, so keep doing independent work.]"
)

#: Generic runtime notice (no dedicated lane).
GENERIC_NOTICE_HEADER = (
    "[RUNTIME NOTICE. This is NOT a user message and NOT a stop request. Read it, then "
    "continue the current task.]"
)

_HEADERS: Dict[str, str] = {
    NOTICE_SUBAGENT: SUBAGENT_SETTLED_HEADER,
    NOTICE_JOB: JOB_SETTLED_HEADER,
    NOTICE_QUESTION: QUESTION_ANSWERED_HEADER,
    NOTICE_RUNTIME: GENERIC_NOTICE_HEADER,
}

#: Stable marker PREFIXES per source - exactly what logic/context_report.py lists as
#: loop-injected (not user speech), and exactly what a persisted notice row starts with
#: (render_notice() puts the header first). Derived from _HEADERS so the notice text and
#: its accounting can never drift apart: a truncated slice of a header would NOT match the
#: list context_report checks with startswith(), so markers are taken from the header's own
#: leading text, not sliced at an arbitrary length.
_MARKERS: Dict[str, str] = {
    NOTICE_SUBAGENT: "[RUNTIME NOTICE - SUBAGENT SETTLED",
    NOTICE_JOB: "[RUNTIME NOTICE - BACKGROUND JOB SETTLED",
    NOTICE_QUESTION: "[RUNTIME NOTICE - THE USER ANSWERED YOUR QUESTION",
    NOTICE_RUNTIME: GENERIC_NOTICE_HEADER[:len("[RUNTIME NOTICE.")],
}

#: The full marker list, in source order. context_report.py mirrors these literals.
NOTICE_MARKERS: Tuple[str, ...] = tuple(_MARKERS.values())


def notice_header_for(source: str) -> str:
    """The header that must precede a notice of this source."""
    key = str(source or "").strip().lower()
    return _HEADERS.get(key, GENERIC_NOTICE_HEADER)


def notice_marker(source: str) -> str:
    """The stable marker prefix context_report.py recognises for this source."""
    key = str(source or "").strip().lower()
    return _MARKERS.get(key, _MARKERS[NOTICE_RUNTIME])


def render_notice(source: str, body: str) -> str:
    """Header + body, ready to append as a user-role message."""
    body = (body or "").strip()
    header = notice_header_for(source)
    return f"{header}\n\n{body}" if body else header


def classify_notice(item: Dict) -> Tuple[str, str]:
    """(source, text) for one queued notice dict. Never raises."""
    if not isinstance(item, dict):
        return NOTICE_RUNTIME, str(item or "")
    source = str(item.get("source") or NOTICE_RUNTIME).strip().lower()
    if source not in _HEADERS:
        source = NOTICE_RUNTIME
    return source, str(item.get("text") or "")


def notice_sources_in(messages: List[Dict]) -> Dict[str, int]:
    """How many notices of each source are currently in the message list.

    Diagnostic helper (the budget report already lists injected notices); kept here so
    the notice vocabulary stays owned by one module.
    """
    counts: Dict[str, int] = {k: 0 for k in _HEADERS}
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        for key, header in _HEADERS.items():
            if content.startswith(header[:24]):
                counts[key] += 1
    return counts
