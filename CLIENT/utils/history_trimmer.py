# utils/history_trimmer.py

"""Atomic-unit history trimmer for the agentic loop.

Trims in ATOMIC UNITS so a tool call and its result always leave together, and has no
fixed message floor: the budget decides. Dropping an assistant message that carries
tool_calls while keeping its tool result produces an orphan the provider rejects
("Failed to parse tool call arguments").
"""

import logging
from typing import List, Dict, Optional

from utils.history_manager import estimate_message_tokens

logger = logging.getLogger("COOLEMS.HistoryTrimmer")


def _is_protected(msg: Dict) -> bool:
    """A message is protected if it carries the `_protect` tag (current user prompt)."""
    return isinstance(msg, dict) and msg.get("_protect") is True


def build_atomic_units(
    messages: List[Dict],
    loop_start: Optional[int] = None,
) -> List[List[int]]:
    """Group messages into atomic units. Returns a list of index lists.
    Unit types (see module docstring):
      - leading "orphan" units: one message each, for everything before the
        first user-role message;
      - "turn" units: [user msg] + all following assistant/tool messages up to
        (but not including) the next user msg.
    Protected messages are NOT merged into a unit — they form their own single-message unit
    so safe_trim_history can skip them individually.
    """
    units: List[List[int]] = []
    current: Optional[List[int]] = None  # open turn (starts with a user msg)
    orphans: List[int] = []              # non-user msgs before the first user msg

    def _flush_orphans() -> None:
        """Emit the leading orphan block as INCREMENTAL atomic units."""
        if not orphans:
            return
        block = list(orphans)
        del orphans[:]

        start = 0
        first_msg = messages[block[0]]
        if isinstance(first_msg, dict) and first_msg.get("role") == "system":
            units.append([block[0]])   # system prompt: own unit, never removable anyway
            start = 1

        i = start
        while i < len(block):
            unit = [block[i]]
            if isinstance(messages[block[i]], dict) and messages[block[i]].get("tool_calls"):
                # Keep the batch intact: assistant(tool_calls) + its tool results.
                j = i + 1
                while j < len(block) and messages[block[j]].get("role") == "tool":
                    unit.append(block[j])
                    j += 1
            units.append(unit)
            i += len(unit)

    # Only messages BEFORE the loop boundary may ever become a removable unit.
    # The current loop (assistant replies + tool results + mid-run steering) is the
    # AI's working context: it stays in the list, never grouped, never planned away.
    limit = len(messages) if loop_start is None else max(0, min(int(loop_start), len(messages)))

    for idx, msg in enumerate(messages[:limit]):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if _is_protected(msg):
            # Close any open turn; protected message stands alone.
            if current:
                units.append(current)
                current = None
            _flush_orphans()
            units.append([idx])
            continue

        if role == "user":
            if current:
                units.append(current)
            _flush_orphans()
            current = [idx]  # start a new turn unit
            continue

        # assistant / tool / system / anything else
        if current is not None:
            current.append(idx)
        else:
            orphans.append(idx)  # leading block, flushed atomically later

    if current:
        units.append(current)
    _flush_orphans()

    return units


def safe_trim_history(
    messages: List[Dict],
    max_tokens: int,
    protected_ids: Optional[List[int]] = None,
    loop_start: Optional[int] = None,
    allow_loop_trim: bool = False,
) -> List[Dict]:
    """Remove oldest atomic units until the message list fits in max_tokens."""
    if not messages:
        return messages

    protected = set(protected_ids or [])
    protected |= {i for i, m in enumerate(messages) if _is_protected(m)}
    protected.add(0)  # system prompt — always kept

    current_tokens = sum(estimate_message_tokens(m) for m in messages)
    if current_tokens <= max_tokens:
        return messages

    # The loop is removable only when the guardian already spoke (see docstring);
    # otherwise its messages are added to the protected set so no unit can cover them.
    loop_protected: set = set()
    if loop_start is not None and not allow_loop_trim:
        loop_protected = {i for i in range(max(0, min(int(loop_start), len(messages))), len(messages))}

    units = build_atomic_units(
        messages,
        loop_start=None if allow_loop_trim else loop_start,
    )
    original_length = len(messages)
    removed_count = 0
    removed_tokens = 0
    remove_idx: set = set()

    removable = protected | loop_protected

    # Decide removals first (whole units, oldest-first), then rebuild the list.
    # Deciding up-front keeps every index valid - no pop-while-indexing bugs.
    for unit in units:
        if current_tokens - removed_tokens <= max_tokens:
            break
        if any(i in removable for i in unit):
            continue  # this unit (or part of it) must stay - skip it entirely

        remove_idx.update(unit)
        removed_count += len(unit)
        removed_tokens += sum(estimate_message_tokens(messages[i]) for i in unit)

    if remove_idx:
        messages[:] = [m for i, m in enumerate(messages) if i not in remove_idx]

    if removed_count > 0:
        new_tokens = sum(estimate_message_tokens(m) for m in messages)
        logger.warning(
            f"[SAFE_TRIM] Removed {removed_count} oldest message(s) "
            f"({len(units)} atomic units considered). "
            f"Tokens: {current_tokens} -> {new_tokens} (limit {max_tokens}). "
            f"Messages: {original_length} -> {len(messages)}"
        )
        if new_tokens > max_tokens:
            logger.warning(
                f"[SAFE_TRIM] Still over budget after removing every removable unit "
                f"({new_tokens} > {max_tokens}) — only protected messages remain"
                + (f" plus the CURRENT LOOP ({len(messages) - int(loop_start)} msgs, "
                   f"loop_start={int(loop_start)})" if loop_protected else "")
                + ". The context-guardian must fire: write a continue plan and stop, "
                  f"so the next loop resumes from user input + AI output + plan."
            )

    return messages

def get_token_count(messages: List[Dict]) -> int:
    """Total estimated tokens (uses per-message cached counts when available)."""
    return sum(estimate_message_tokens(m) for m in messages)
