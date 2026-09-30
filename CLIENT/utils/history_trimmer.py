# utils/history_trimmer.py

"""
Atomic-unit history trimmer for the agentic loop.

WHY THIS EXISTS (root cause of the "Failed to parse tool call arguments" 500s):
    The old trimmer removed SINGLE messages one-by-one and refused to go below
    a fixed 50-message floor. In a long agentic run that meant:
      * trimming silently no-op'd once >50 messages existed, so the prompt grew
        unbounded until llama.cpp ran out of room;
      * when it did trim, it could delete an assistant message with `tool_calls`
        while leaving its `role:"tool"` results behind (or vice versa) — an
        invalid OpenAI-format history that confuses the model.

NEW RULES:
    1. The SYSTEM PROMPT is never removed.
    2. The CURRENT USER PROMPT (the message the user just sent, tagged with
       `_protect: True` by build_messages()) is never removed — it must remain
       at all times so the AI always knows what it was asked to do.
    3. Everything else is grouped into ATOMIC UNITS and only whole units are
       ever removed (oldest first). A unit can never be split, which guarantees
       an assistant message with `tool_calls` always stays together with every
       one of its tool results:

        - "turn"    : [user msg] + the AI reply that answered it
                      (assistant text and/or assistant+tool_calls) plus ALL tool
                      results belonging to that reply, up to just before the
                      next user message.
        - "orphan"  : leading messages before the first user message — each one
                      is its own unit so nothing can be stranded.

    4. There is NO minimum-message floor: if the budget requires it, ALL units
       may be removed and only [system prompt + current user prompt] remain.
       That is exactly what "trim better" means for this loop.

TOKEN CACHING:
    Per-message token counts are cached on the message dict itself by
    estimate_message_tokens() (key `_est_tokens`), so the total is a cheap sum
    of ints — no re-tokenization and no base64 regex scans per iteration.
"""

import logging
from typing import List, Dict, Optional

from utils.history_manager import estimate_message_tokens

logger = logging.getLogger("COOLEMS.HistoryTrimmer")


def _is_protected(msg: Dict) -> bool:
    """A message is protected if it carries the `_protect` tag (current user prompt)."""
    return isinstance(msg, dict) and msg.get("_protect") is True


def build_atomic_units(messages: List[Dict]) -> List[List[int]]:
    """Group messages into atomic units. Returns a list of index lists.

    Unit types (see module docstring):
      - leading "orphan" units: one message each, for everything before the
        first user-role message;
      - "turn" units: [user msg] + all following assistant/tool messages up to
        (but not including) the next user msg.

    Protected messages are NOT merged into a unit — they form their own
    single-message unit so safe_trim_history() can skip them individually.
    """
    units: List[List[int]] = []
    current: Optional[List[int]] = None  # open turn (starts with a user msg)
    orphans: List[int] = []              # non-user msgs before the first user msg

    def _flush_orphans() -> None:
        """Emit the leading orphan block.

        A lone leading SYSTEM message is emitted as its own unit (it is
        protected by safe_trim_history() anyway); any remaining orphans are
        merged into ONE atomic unit so an assistant tool_calls message can
        never be separated from its tool results — even for messages that were
        loaded without their original user prompt.
        """
        if not orphans:
            return
        block = list(orphans)
        del orphans[:]
        first_msg = messages[block[0]]
        if isinstance(first_msg, dict) and first_msg.get("role") == "system":
            units.append([block[0]])
            if len(block) > 1:
                units.append(block[1:])
        else:
            units.append(block)

    for idx, msg in enumerate(messages):
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
) -> List[Dict]:
    """Remove oldest atomic units until the message list fits in max_tokens.

    Args:
        messages: The live message list (modified in place). Index 0 is assumed
            to be the system prompt — it is never removed. Messages tagged with
            `_protect` are also never removed. `protected_ids` may additionally
            name indices that must survive (e.g. the current user prompt when
            the caller did not tag it).
        max_tokens: Hard token budget for the WHOLE list (system + history +
            current user prompt included). Callers should pass a value that
            already leaves room for the tool schema and the model's output.

    Returns:
        The same list object, trimmed in place.

    Behavior:
        - Never removes index 0 (system prompt) or any protected message.
        - Removes whole atomic units only, oldest first — an assistant
          tool_calls message can never be separated from its tool results.
        - No minimum-message floor: if needed, everything between the system
          prompt and the current user prompt is removed.
    """
    if not messages:
        return messages

    protected = set(protected_ids or [])
    protected |= {i for i, m in enumerate(messages) if _is_protected(m)}
    protected.add(0)  # system prompt — always kept

    current_tokens = sum(estimate_message_tokens(m) for m in messages)
    if current_tokens <= max_tokens:
        return messages

    units = build_atomic_units(messages)
    original_length = len(messages)
    removed_count = 0
    removed_tokens = 0
    remove_idx: set = set()

    # Decide removals first (whole units, oldest-first), then rebuild the list.
    # Deciding up-front keeps every index valid - no pop-while-indexing bugs.
    for unit in units:
        if current_tokens - removed_tokens <= max_tokens:
            break
        if any(i in protected for i in unit):
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
                f"({new_tokens} > {max_tokens}) — only protected messages remain. "
                f"The provider's own context guard will clamp generation."
            )

    return messages


def is_over_token_limit(messages: List[Dict], max_tokens: int) -> bool:
    """Quick check if messages exceed token limit without modifying."""
    current_tokens = sum(estimate_message_tokens(m) for m in messages)
    return current_tokens > max_tokens


def get_token_count(messages: List[Dict]) -> int:
    """Total estimated tokens (uses per-message cached counts when available)."""
    return sum(estimate_message_tokens(m) for m in messages)
