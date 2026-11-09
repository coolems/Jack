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
    3. THE CURRENT LOOP IS NEVER REMOVED (2026-11-08, user requirement): while a
       ReAct loop is running, everything the loop itself appended — its assistant
       replies, tool_calls and tool results, plus any steering message injected
       mid-run — is the AI's working context. Trimming may only eat history that
       sits BEFORE the loop boundary (the previous turns of the session). When the
       loop alone cannot fit, trimming reports "nothing removable" and the
       context-guardian fires: the AI writes plan_<...>_continue.md and stops, and
       the NEXT loop starts from user input + AI output + that plan. Only after the
       guardian has spoken may a caller pass `allow_loop_trim=True` to shrink the
       loop (so the wrap-up answer itself has room).
    4. Everything else is grouped into ATOMIC UNITS and only whole units are
       ever removed (oldest first). A unit can never be split, which guarantees
       an assistant message with `tool_calls` always stays together with every
       one of its tool results:

        - "turn"    : [user msg] + the AI reply that answered it
                      (assistant text and/or assistant+tool_calls) plus ALL tool
                      results belonging to that reply, up to just before the
                      next user message.
        - "orphan"  : messages with no user prompt in front of them (leading history, and
                      everything AFTER the current user prompt, which is what a running loop
                      holds) — split into the smallest contract-safe units: assistant+tool_calls
                      glued to its tool results, everything else one message per unit.
                      Never merged into one block: that made trimming all-or-nothing.

    5. There is NO minimum-message floor: if the budget requires it, ALL removable
       units may be removed and only [system prompt + current user prompt + the
       protected current loop] remain. That is exactly what "trim better" means
       for this loop.

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

    Protected messages are NOT merged into a unit — they form their own
    single-message unit so safe_trim_history() can skip them individually.

    `loop_start` (2026-11-08): the index where the CURRENT ReAct loop begins -
    everything the loop appended after build_messages() (assistant replies,
    tool_calls, tool results, mid-run steering). Those indices are NOT emitted as
    units at all, so a trim can never plan a removal inside the running loop.
    Pass None to keep the old all-messages behaviour.
    """
    units: List[List[int]] = []
    current: Optional[List[int]] = None  # open turn (starts with a user msg)
    orphans: List[int] = []              # non-user msgs before the first user msg

    def _flush_orphans() -> None:
        """Emit the leading orphan block as INCREMENTAL atomic units.

        FIX (2026-11-08): the old version merged the whole block into ONE unit. That block is
        everything sitting AFTER the current user prompt (the protected message closes the
        open turn), so inside a running loop it holds every assistant reply and tool result of
        the current turn - tens of thousands of tokens once big tool outputs are in it. With
        whole-unit removal that left only two outcomes: remove nothing, or gut the entire turn
        (observed in CLIENT/logs/coolems.log iter=13: 207278 -> 3268 tokens, 43 -> 2 messages,
        204,010 tokens discarded in one pass). That is a discard, not trimming, and it also
        made the context-guardian unreachable: stage 3 only fires when a trim freed NO room,
        so a gut trim silently swallowed the run instead of making the AI write a continue
        plan and stop.

        The block is now split into the smallest units the message contract allows, oldest
        first, so trimming frees room gradually:
          - a leading `system` message is its own unit (protected by safe_trim_history anyway);
          - an assistant message carrying `tool_calls` is glued to ALL the `role:"tool"`
            results that follow it — that pair can never be separated;
          - every other message is its own unit.
        """
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
        loop_start: Index where the CURRENT ReAct loop begins (everything the loop
            appended after build_messages()). Those messages are never removed.
            Pass None for the old whole-list behaviour.
        allow_loop_trim: Only a caller that has ALREADY fired the context guardian
            (so a continue plan exists) may set this - it lets the trim shrink the
            loop too, to give the wrap-up answer room. Default False.

    Returns:
        The same list object, trimmed in place.

    Behavior:
        - Never removes index 0 (system prompt) or any protected message.
        - Never removes the current loop unless `allow_loop_trim` is set.
        - Removes whole atomic units only, oldest first — an assistant
          tool_calls message can never be separated from its tool results.
        - No minimum-message floor for the removable part: if needed, everything
          between the system prompt and the loop boundary is removed.
    """
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


def is_over_token_limit(messages: List[Dict], max_tokens: int) -> bool:
    """Quick check if messages exceed token limit without modifying."""
    current_tokens = sum(estimate_message_tokens(m) for m in messages)
    return current_tokens > max_tokens


def get_token_count(messages: List[Dict]) -> int:
    """Total estimated tokens (uses per-message cached counts when available)."""
    return sum(estimate_message_tokens(m) for m in messages)
