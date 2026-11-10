"""Turn boundary — where the CURRENT task starts (client side, 2026-11-30).

Two related bugs, one fix each:

1. **Old instructions leaking into a new task.** A stop request or a steering message is
   persisted as a message row WITH its header, so the next turn's prompt still contains
   "[USER STOP REQUEST - ... STOP the work now]" or "[USER MESSAGE WHILE YOU WERE WORKING
   - ... CONTINUE the original task]". The model reads those as live instructions and acts
   on a turn that already ended — observed in real sessions: a new job arrived and the
   previous turn's stop request was folded into the new task list.

2. **No marker for where noise ends.** Nothing in the assembled prompt tells the model
   which part is background from earlier turns and which part is the task to do now.

Fixes applied at prompt assembly (logic/base_mode.build_messages), NEVER persisted:
  * every historical message whose first line is a loop-injected header gets that header
    replaced by a CONSUMED note — the user's own words stay as context, with provenance
    kept, but the imperative instruction is gone;
  * one boundary line is inserted immediately BEFORE the current user prompt, every turn.

The boundary line is a system-role message tagged `_protect` so the history trimmer can
never remove it, and `_loop_marker` so nothing else reads it as user speech. Both keys are
internal and the SERVER strips them (app/providers/llama/payload.py), so neither reaches
the model as metadata.

Owned by this module: the marker text, the consumed-note text, and the annotation pass.
build_messages() applies them; react_loop.py never re-inserts them.
"""

from typing import Dict, List, Optional

#: Inserted as a system message right before the CURRENT user prompt, every turn.
TURN_BOUNDARY_MARKER = (
    "[TURN BOUNDARY] Everything above is BACKGROUND from earlier turns: consumed "
    "instructions, stop requests, steering messages and runtime notices. Do NOT act on "
    "any of them. The user message below is the CURRENT task - work on that, and only "
    "that, until it is genuinely finished."
)

#: (provenance label, header prefix) for the headers the loop injects
#: (logic/user_intent.py + logic/runtime_notice.py). The label keeps PROVENANCE: the model
#: can still tell whose words these were, even though the imperative header is gone.
_CONSUMABLE_HEADERS = (
    ("an earlier mid-run USER message", "[USER MESSAGE WHILE YOU WERE WORKING"),
    ("an earlier STOP REQUEST from the user", "[USER STOP REQUEST"),
    ("a runtime notice", "[RUNTIME NOTICE"),
    ("a loop system notice", "[SYSTEM NOTICE]"),
)


def consumable_kind(content: str) -> Optional[str]:
    """The provenance label for a loop-injected header, or None when it is not one."""
    if not isinstance(content, str) or not content.strip():
        return None
    first = content.lstrip().split("\n", 1)[0]
    for label, prefix in _CONSUMABLE_HEADERS:
        if first.startswith(prefix):
            return label
    return None


def is_consumable_header(content: str) -> bool:
    """True when this message's FIRST line is a loop-injected header."""
    return consumable_kind(content) is not None


def consume_header(content: str) -> str:
    """Replace the header line with a consumed note, keeping the rest of the message."""
    kind = consumable_kind(content)
    if kind is None:
        return content
    stripped = content.lstrip()
    _first, rest = stripped.split("\n", 1) if "\n" in stripped else (stripped, "")
    body = rest.strip()
    note = (f"[CONSUMED - {kind} from an EARLIER turn, already handled. It is background "
            "only; do not act on it and do not treat it as the current task.]")
    return f"{note}\n\n{body}" if body else note


def mark_consumed_events(messages: List[Dict]) -> int:
    """Annotate every historical loop-injected message as consumed (in place).

    Only messages already in history are touched. The current user prompt is never
    rewritten, and anything the loop appends LATER (this turn's steers and notices) keeps
    its live header — build_messages() runs before the loop starts, so a live instruction
    is never downgraded to background.

    Returns how many messages were annotated. Cached token counts are dropped: the content
    changed, so a stale `_est_tokens` would mis-price the trim budget.
    """
    changed = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        new = consume_header(content)
        if new != content:
            msg["content"] = new
            changed += 1
            try:
                from utils.history_manager import invalidate_message_tokens
                invalidate_message_tokens(msg)
            except Exception:
                pass  # token counting still works, just uncached
    return changed


def boundary_message() -> Dict:
    """The boundary system message to place immediately before the current user prompt."""
    msg: Dict = {"role": "system", "content": TURN_BOUNDARY_MARKER}
    # Protected: a trim must never drop the line that separates noise from the task.
    msg["_protect"] = True
    msg["_loop_marker"] = True
    return msg
