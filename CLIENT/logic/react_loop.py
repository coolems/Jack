"""
ReAct (Reasoning + Acting) loop control for agentic mode.


Handles:
- Main tool iteration loop with provider-native tool calling
- Token stats pushing after every provider call
- Tool result processing and message history management
- Stop event checking between iterations
- Live steering: user messages typed during a run are injected as fresh user turns at
  safe boundaries (before a provider call / before pending tool calls)
- Safe-margin token warning + context-guardian finish/plan message before hard trim
- Temp thinking array to preserve all AI reasoning steps in final history

FIXED (2026-01-XX): Restored loop exit logic from working version.
FIXED (2026-01-XX): ToolCompilationError now terminates loop immediately without retry.
"""


import asyncio
import json
import logging
import re
import time
from typing import List, Dict, Any, Tuple, Optional

from config import (
    AGENTIC_MAX_TOOL_ITERATIONS,
    AGENTIC_TOKEN_WARNING_THRESHOLD,
    AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS,
)




from utils.history_manager import (
    max_history_tokens,
    invalidate_message_tokens,
    estimate_message_tokens,
)
from utils.history_trimmer import safe_trim_history, get_token_count

# ============================================================
# LEARNED LESSONS - DISABLED ON PURPOSE (dead code kept on purpose).
# Will be fixed later: we are coming back to this. For now the whole lessons
# pipeline is skipped so it neither inflates the system prompt nor complicates
# history trimming. The old logic stays in place as dead code below - re-enable
# by setting _LESSONS_INJECTION_ENABLED = True.
# TODO(code-review C7, plan_20261102_2135_code_review.md): re-enable with the
# lessons text counted into the system-prompt cost (it rides in the system message, so
# it must be measured like the tool schema) before flipping the flag.
# ============================================================
_LESSONS_INJECTION_ENABLED = False


from .base_mode import push_token_stats, check_stop_event, build_session_stats, strip_turn_stats_lines
from .provider_call import call_provider_with_retry
from .tool_lock import tool_lock, SCREEN_BROWSER_TOOLS


logger = logging.getLogger("COOLEMS.Logic.ReactLoop")


# ============================================================
# LIVE STEERING (2026-10-23, intent rules 2026-11)
# While an agentic turn is running the UI can hand the loop a new user instruction
# ("steer"). It is injected as a USER message at the next safe boundary - before a provider
# call, or right before pending tool calls - so the SAME task continues with the correction
# instead of being stopped and restarted.
#
# The header is chosen by intent (logic.user_intent):
#   * interjection ("tell me the current time")  -> answer it, then CONTINUE the task
#   * explicit stop ("stop the review")          -> end the task
# A mid-run question must never close the job. A text-only reply ends the turn, so the
# FIRST text-only reply after an interjection is DEFERRED (see _interjection_pending
# handling below) unless the same batch also asked for a stop.
# ============================================================
from .user_intent import (
    STEER_INTERJECTION_HEADER,
    STEER_STOP_HEADER,
    SteerOutcome,
    classify_steers,
    interjection_resume_notice,
    steer_header_for,
)

TOOL_SKIPPED_FOR_STEER = (
    "NOT EXECUTED: the user sent a steering message while this tool call was pending, so it was "
    "re-planned before running. Read the newest user instruction above and decide whether this "
    "call is still needed."
)

# (2026-11-30) A batch call that never STARTED is cancelled on a stop, so the skipped text
# can be truthful. A call that already began its side effects is never cancelled - it gets
# its real result, whatever the stop did to the rest of the batch.
TOOL_SKIPPED_FOR_STOP = (
    "NOT EXECUTED: the user stopped this turn before the call started, so it never ran and "
    "no side effect happened."
)

# How often a flush re-checks for a stop/steer while waiting on launched calls. A flush
# that only awaits would be blind to a stop until every launched call finished.
_INTERRUPT_POLL_SEC = 0.25

# A reply with neither text nor tool calls is a provider hiccup, not a thinking step:
# ask again up to this many times, then deliver an honest fallback instead of spinning.
MAX_EMPTY_REPLIES = 2


def _has_pending_steers(config: Any) -> bool:
    """True when the chat channel holds a steering message NOT yet injected.

    `has_unapplied_steer()` is the gate, not `has_pending_steer()`: an already-injected
    steer must never trigger another pre-emption / second injection.
    """
    source = getattr(config, "steer_source", None)
    gate = getattr(source, "has_unapplied_steer", None) or getattr(source, "has_pending_steer", None)
    if not callable(gate):
        return False
    try:
        return bool(gate())
    except Exception:  # pragma: no cover - steering must never break the loop
        return False


async def _apply_pending_steers(config: Any, messages: List[Dict]) -> SteerOutcome:
    """Drain queued steering messages and append them to *messages* as user turns.

    Ordering contract: callers that are about to inject after a streamed assistant answer
    must append that assistant message FIRST, then call this helper - so the steer is the
    final user message the next provider call sees.

    Side effects per message (best-effort, never fatal):
      * the steer id is marked applied on the source, so a second drain injects nothing;
      * `config.on_steer_applied(item)` persists the raw text exactly once, in order;
      * a `steer_applied` frame settles the UI bubble (correlated by steer id, carrying
        `at_ts` = the moment the user typed it);
      * `config.cycle.steer(text, seq, at_ts)` records ONE cycle-history row per steer.

    Returns a SteerOutcome (int-compatible count + interjection/stop classification).
    """
    source = getattr(config, "steer_source", None)
    drain = getattr(source, "drain_steer", None)
    if not callable(drain):
        return SteerOutcome(0)
    try:
        items = drain() or []
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[STEER] Could not drain steering queue: {e}")
        return SteerOutcome(0)

    callback = getattr(config, "on_steer_applied", None)
    cycle = getattr(config, "cycle", None)
    applied = 0
    texts: List[str] = []
    seen_ids: set = set()

    for item in items:
        if isinstance(item, str):
            text, steer_id = item, None
        elif isinstance(item, dict):
            text = item.get("text", "")
            steer_id = item.get("id")
        else:
            continue
        text = (text or "").strip()
        if not text:
            continue
        # Same id twice (drain race) must inject ONE user turn, not two.
        if steer_id is not None:
            key = str(steer_id)
            if key in seen_ids:
                continue
            seen_ids.add(key)

        # The placement time is when the user typed it, never the injection instant -
        # the UI sorts a step group's rows by this value.
        at_ts = item.get("received_at") if isinstance(item, dict) else None

        # Persist BEFORE injection so the DB order matches the order the model sees, and
        # a message that never reached the model is never written (see bus.steer docstring).
        if callable(callback):
            try:
                await callback(item)
            except Exception as e:
                logger.error(f"[STEER] Failed to persist applied steering message {steer_id}: {e}")

        mark = getattr(source, "mark_steer_applied", None)
        if callable(mark):
            try:
                mark(steer_id)
            except Exception:
                pass  # bookkeeping only - injection still happened

        header = steer_header_for(text)
        messages.append({"role": "user", "content": f"{header}\n\n{text}"})
        applied += 1
        texts.append(text)

        # turn_id rides on the ack so the UI can route it into the RUNNING turn's
        # loop-history block (steering is never a chat-level message).
        if getattr(config, "websocket", None):
            try:
                await config.websocket.send_text(json.dumps({
                    "type": "steer_applied",
                    "steer_id": steer_id,
                    "turn_id": getattr(cycle, "turn_id", "") or "",
                    "content": text,
                    "at_ts": at_ts,
                }))
            except Exception:
                pass  # UI gone - injection still happened

        if cycle is not None:
            try:
                await cycle.steer(text, seq=applied, at_ts=at_ts,
                                  steer_id=str(steer_id or ""))
            except Exception:
                pass  # history is best-effort

        logger.info(f"[STEER] Applied steering message {steer_id} at loop boundary "
                    f"(chars={len(text)}, total_applied_now={applied})")

    interjection, stop = classify_steers(texts)
    return SteerOutcome(applied, interjection=interjection, stop=stop)


# ============================================================
# RUNTIME NOTICES (2026-12 subagents + background jobs + questions)
# The runtime's own lane, beside live steering. Same injection boundaries, but:
#   * a notice NEVER sets interjection_pending (it is information, not a request),
#   * a notice arriving during a FINAL answer RE-OPENS the turn instead of deferring it:
#     "a delegated child finished" is new evidence, and the turn must act on it,
#   * notices are persisted with their header kept (messages.is_notice) so a later turn
#     can still tell who said what.
# Sources + headers live in logic/runtime_notice.py; this helper only applies them.
# ============================================================
from .runtime_notice import classify_notice, render_notice

#: (2026-11-30) How many times a notice arriving during a FINAL answer may re-open the
#: turn. A notice is new evidence, so the answer must not be accepted while one waits -
#: but an unbounded re-open (children settling one after another) costs a provider call
#: each time and burns AGENTIC_MAX_TOOL_ITERATIONS with no progress. Past the budget the
#: answer is accepted and the remaining notices ride the notice lane (a follow-up turn,
#: itself bounded by NOTICE_MAX_CONSECUTIVE_WAKES), so nothing is lost.
MAX_NOTICE_REOPENS = 3


def _has_pending_notices(config: Any) -> bool:
    """True when the channel holds a runtime notice NOT yet injected.

    Same gate rule as steering: an already-injected notice must never fire a second
    injection, so has_unapplied_notice() is the gate (never a bare queue length).
    """
    source = getattr(config, "notice_source", None)
    gate = getattr(source, "has_unapplied_notice", None)
    if not callable(gate):
        return False
    try:
        return bool(gate())
    except Exception:  # pragma: no cover - the notice lane must never break the loop
        return False


async def _apply_pending_notices(config: Any, messages: List[Dict]) -> int:
    """Drain queued runtime notices and append them as user-role messages.

    Side effects per notice (best-effort, never fatal), mirroring _apply_pending_steers:
      * the id is marked applied on the source, so a second drain injects nothing;
      * config.on_notice_applied(item) persists it exactly once, BEFORE injection, so the
        DB order matches the order the model sees;
      * a `notice_applied` frame settles the UI row (correlated by notice id);
      * config.cycle.notice(...) records ONE cycle-history row per notice.

    Returns the number injected. Callers that inject after a streamed assistant answer
    must append that answer FIRST, so the notice is the newest message the model sees.
    """
    source = getattr(config, "notice_source", None)
    drain = getattr(source, "drain_notice", None)
    if not callable(drain):
        return 0
    try:
        items = drain() or []
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[NOTICE] Could not drain the notice queue: {e}")
        return 0

    callback = getattr(config, "on_notice_applied", None)
    cycle = getattr(config, "cycle", None)
    applied = 0
    seen_ids: set = set()

    for item in items:
        if isinstance(item, str):
            source_kind, text = "runtime", item
            notice_id = None
        elif isinstance(item, dict):
            source_kind, text = classify_notice(item)
            notice_id = item.get("id")
        else:
            continue
        text = (text or "").strip()
        if not text:
            continue
        # Same id twice (drain race) must inject ONE notice, not two - a duplicated
        # settlement would make the model believe a child finished twice.
        if notice_id is not None:
            key = str(notice_id)
            if key in seen_ids:
                continue
            seen_ids.add(key)

        at_ts = item.get("received_at") if isinstance(item, dict) else None

        if callable(callback):
            try:
                await callback(item)
            except Exception as e:
                logger.error(f"[NOTICE] Failed to persist applied notice {notice_id}: {e}")

        mark = getattr(source, "mark_notice_applied", None)
        if callable(mark):
            try:
                mark(notice_id)
            except Exception:
                pass  # bookkeeping only - injection still happened

        messages.append({"role": "user", "content": render_notice(source_kind, text)})
        applied += 1

        # turn_id rides on the ack so the UI routes it into the RUNNING turn's block.
        if getattr(config, "websocket", None):
            try:
                await config.websocket.send_text(json.dumps({
                    "type": "notice_applied",
                    "notice_id": notice_id,
                    "source": source_kind,
                    "turn_id": getattr(cycle, "turn_id", "") or "",
                    "content": text[:600],
                    "at_ts": at_ts,
                }))
            except Exception:
                pass  # UI gone - injection still happened

        if cycle is not None:
            try:
                await cycle.notice(text, source=source_kind, seq=applied, at_ts=at_ts,
                                   notice_id=str(notice_id or ""))
            except Exception:
                pass  # history is best-effort

        logger.info(f"[NOTICE] Applied {source_kind} notice {notice_id} at loop boundary "
                    f"(chars={len(text)}, total_applied_now={applied})")

    return applied


# ============================================================
# TOOL BATCH DISPATCH (2026-12 parallel tools)
# The old loop ran every tool of a batch sequentially, so 4 background jobs started one
# per iteration and 3 subagents serialized. Two tiers now:
#   * SCREEN_BROWSER_TOOLS stay sequential (the physical screen / shared browser are
#     singleton resources - tool_lock serialises them across chats anyway);
#   * everything else (files, search, web, python, subagents, job controls) runs
#     concurrently under a semaphore.
# ORDER RULE: results are appended in the model's ORIGINAL call order, one role="tool"
# message per tool_call id - always, even for calls a stop or a steer skipped. A
# screen/browser call awaits every tool launched BEFORE it, so side effects still happen
# in the order the model asked for; concurrency is only between independent calls.
# (2026-11-30) The barrier itself is LAUNCHED, not awaited inline: calls after it are
# independent of it, so a slow screenshot or goto no longer stalls the rest of the batch.
# Two screen calls in one batch still run one at a time - the second barrier flushes the
# first, and tool_lock is re-entrant per owner, so the lock alone would not serialise them.
# ============================================================
from config import TOOL_MAX_PARALLEL

def _append_skipped_tool_results(messages: List[Dict], tool_calls: List[Dict], start_idx: int,
                                 text: str = TOOL_SKIPPED_FOR_STEER) -> int:
    """Complete every pending tool call with an explicit 'not executed' result.

    The OpenAI message contract REQUIRES one role="tool" message for every tool_call id
    on the preceding assistant message. When steering pre-empts a batch we must keep that
    contract valid or the next provider call is rejected - so each skipped call gets a
    synthetic result explaining why it did not run.
    """
    skipped = 0
    for tc in tool_calls[start_idx:]:
        tool_id = tc.get("id", "") if isinstance(tc, dict) else ""
        messages.append({
            "role": "tool",
            "content": text,
            "tool_call_id": tool_id,
        })
        skipped += 1
    return skipped


# Lazy-import ToolCompilationError to avoid circular dependency at module load time
_TOOL_COMPILATION_ERROR: Any = None
_TOOL_COMPILATION_ERROR_RESOLVED = False


async def _run_one_tool_call(tc: Dict, tool_executor: Any, config: Any,
                             conversation_history: list,
                             live_messages: List[Dict]) -> Tuple[str, str, str]:
    """Execute ONE tool call without appending to *messages*. Returns (tool_call_id, text, lessons).

    Split out of _execute_single_tool_call (2026-12) because concurrent dispatch must
    append results in the model's ORIGINAL call order: a task that appended its own
    result would land in completion order, which breaks the tool_call_id pairing the
    provider contract requires. Screen/browser tools still take the singleton tool_lock.

    live_messages is still passed (2026-08-27 rule): tool-viewed image pixels are
    sanitized out of the result text and re-attached to the PROTECTED current-user
    prompt of the list that is actually sent to the provider. That is a dict mutation,
    not a list append, so concurrent calls cannot reorder anything.
    """
    func = tc.get("function", {}) if isinstance(tc, dict) else {}
    tool_name = func.get("name", "unknown") if isinstance(func, dict) else "unknown"
    tool_id = tc.get("id", "")

    try:
        args_str = func.get("arguments", "{}") if isinstance(func, dict) else "{}"
        if isinstance(args_str, str):
            try:
                parsed_args = json.loads(args_str)
            except json.JSONDecodeError:
                logger.warning(f"[AGENTIC.DEBUG] react_loop - Malformed JSON in tool arguments for {tool_name}, attempting repair")
                parsed_args = _repair_json_arguments(args_str)
                if not parsed_args:
                    raise ValueError(f"Could not parse/repair tool arguments: {args_str[:200]}")
        else:
            parsed_args = args_str
    except Exception as e:
        logger.error(f"[AGENTIC.DEBUG] react_loop - Failed to parse arguments for {tool_name}: {e}")
        # Tell the model WHAT to do instead of echoing its own broken JSON back at it.
        # The usual cause is the output token limit cutting the tool-call JSON mid-string.
        return tool_id, (
            f"Tool argument parsing failed for '{tool_name}': malformed or truncated JSON in tool arguments "
            '(likely hit the output token limit). Do NOT retry the same large payload - re-issue this'
            ' call with a SMALLER argument (e.g. split the code into multiple smaller python_exec calls)'
        ), ""

    try:
        # (2026-09-08 multi-chat) Screen/browser tools are serialized across chats: two
        # agentic loops must not fight over the same mouse/keyboard at once.
        _owner = getattr(config, 'conversation_id', None) or '_unknown'
        if tool_name in SCREEN_BROWSER_TOOLS:
            async with tool_lock(_owner):
                result_text, new_lessons = await tool_executor.execute(
                    tool_name=tool_name,
                    tool_args=parsed_args,
                    conversation_history=conversation_history,
                    websocket=config.websocket,
                    live_messages=live_messages,
                    conversation_id=getattr(config, 'conversation_id', None),
                )
        else:
            result_text, new_lessons = await tool_executor.execute(
                tool_name=tool_name,
                tool_args=parsed_args,
                conversation_history=conversation_history,
                websocket=config.websocket,
                live_messages=live_messages,
                conversation_id=getattr(config, 'conversation_id', None),
            )
        return tool_id, str(result_text), (new_lessons or "")
    except Exception as e:
        logger.error(f"[AGENTIC.DEBUG] react_loop - Tool execution failed for {tool_name}: {e}")
        return tool_id, f"Tool execution error: {str(e)[:500]}", ""


async def _execute_tool_batch(tool_calls: List[Dict], tool_executor: Any, config: Any,
                              messages: List[Dict], conversation_history: list,
                              agent_lessons: str, start_idx: int = 0) -> str:
    """Run the remaining tool calls of one batch. Returns the merged lessons string.

    Two tiers (see the TOOL BATCH DISPATCH header):
      * a screen/browser call is a barrier - every call launched before it is awaited
        first, so side effects still happen in the order the model asked for;
      * independent calls run concurrently under TOOL_MAX_PARALLEL.

    Every tool_call id gets exactly one role="tool" message, in the model's original
    order, including the calls a stop or a steer skipped (explicit NOT EXECUTED text).
    """
    sem = asyncio.Semaphore(max(1, TOOL_MAX_PARALLEL))
    # (idx, started_flag, task). started_flag says whether the call has begun its side
    # effects - a task still waiting on the semaphore can be cancelled honestly, a call
    # that already started cannot be recalled.
    inflight: List[Tuple[int, List[bool], asyncio.Task]] = []

    async def _guarded(tc: Dict, started: List[bool]) -> Tuple[str, str, str]:
        async with sem:
            started[0] = True
            return await _run_one_tool_call(tc, tool_executor, config,
                                            conversation_history, messages)

    def _interrupt_reason() -> Optional[str]:
        """'stop' | 'steer' | None - re-checked while a flush is waiting."""
        if check_stop_event(config) is not None:
            return "stop"
        if _has_pending_steers(config):
            return "steer"
        return None

    async def _flush() -> bool:
        """Await the launched calls; append results in ORIGINAL call order.

        Returns True when a stop or a steer pre-empted the flush. Calls that had NOT
        started yet are cancelled (no side effect happened, so the skipped text is
        truthful); calls that already started are awaited to completion - they cannot be
        recalled, and their result must say what they actually did.
        """
        nonlocal agent_lessons
        if not inflight:
            return False
        entries = list(inflight)
        inflight.clear()
        pending = [t for _, _, t in entries]
        interrupted = False
        reason: Optional[str] = None
        while pending:
            _done, pending = await asyncio.wait(pending, timeout=_INTERRUPT_POLL_SEC)
            if not pending:
                break
            reason = _interrupt_reason()
            if reason is not None:
                interrupted = True
                for _idx, started, t in entries:
                    if not started[0] and t in pending:
                        t.cancel()
                # Await EVERYTHING still pending: started calls finish (their side
                # effects already happened), cancelled calls complete with
                # CancelledError. Leaving a task unfinished would make task.result()
                # raise InvalidStateError in the append loop below.
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                break
        for idx, _started, task in sorted(entries, key=lambda e: e[0]):
            tc = tool_calls[idx]
            tool_id = tc.get("id", "") if isinstance(tc, dict) else ""
            name = tc.get("function", {}).get("name", "unknown") if isinstance(tc, dict) else "unknown"
            if task.cancelled():
                messages.append({"role": "tool",
                                 "content": TOOL_SKIPPED_FOR_STOP if reason == "stop"
                                 else TOOL_SKIPPED_FOR_STEER,
                                 "tool_call_id": tool_id})
                continue
            try:
                _rid, text, lessons = task.result()
                if lessons:
                    agent_lessons = (agent_lessons + "\n\n" + lessons).strip() if agent_lessons else lessons
            except asyncio.CancelledError:  # pragma: no cover - handled above
                text = TOOL_SKIPPED_FOR_STOP
            except BaseException as exc:
                text = f"Tool execution error: {str(exc)[:500]}"
                logger.error(f"[AGENTIC.DEBUG] react_loop - Tool {name} raised: {exc}")
            messages.append({"role": "tool", "content": str(text), "tool_call_id": tool_id})
        return interrupted

    for tc_idx in range(start_idx, len(tool_calls)):
        tc = tool_calls[tc_idx]
        if check_stop_event(config) is not None:
            await _flush()   # launched calls still need their result messages
            _skipped = _append_skipped_tool_results(messages, tool_calls, tc_idx)
            logger.warning("[AGENTIC.DEBUG] react_loop - STOP EVENT set mid-batch; "
                           f"skipping {len(tool_calls) - tc_idx} remaining tool call(s)")
            break

        # ---- LIVE STEERING pre-empts PENDING (not yet launched) side effects ----
        # Calls already launched cannot be recalled, so they are flushed first; the
        # remaining ones get explicit "not executed" results before the steer is injected.
        if _has_pending_steers(config):
            await _flush()
            _skipped = _append_skipped_tool_results(messages, tool_calls, tc_idx)
            _steers_applied = await _apply_pending_steers(config, messages)
            logger.warning(
                f"[AGENTIC.DEBUG] react_loop - steering pre-empted {_skipped} pending "
                f"tool call(s) at index {tc_idx}; injected {_steers_applied} steering message(s)"
            )
            break

        func = tc.get("function", {}) if isinstance(tc, dict) else {}
        tool_name = func.get("name", "unknown") if isinstance(func, dict) else "unknown"

        if tool_name in SCREEN_BROWSER_TOOLS:
            # Singleton resource: an ORDER barrier. Await everything launched before it so
            # the model's requested order of side effects is preserved.
            await _flush()
            # A steer or a Stop that arrived DURING the flush outranks the barrier call
            # that has not started yet: the user's new instruction must not be overtaken by
            # a mouse click the model asked for before it. (Calls already flushed cannot be
            # recalled - they keep their real results.)
            if check_stop_event(config) is not None:
                _skipped = _append_skipped_tool_results(messages, tool_calls, tc_idx)
                logger.warning("[AGENTIC.DEBUG] react_loop - STOP EVENT set while flushing "
                               "a screen/browser barrier; "
                               f"skipping {len(tool_calls) - tc_idx} remaining tool call(s)")
                break
            if _has_pending_steers(config):
                _skipped = _append_skipped_tool_results(messages, tool_calls, tc_idx)
                _steers_applied = await _apply_pending_steers(config, messages)
                logger.warning(
                    f"[AGENTIC.DEBUG] react_loop - steering pre-empted {_skipped} pending "
                    f"tool call(s) at the barrier index {tc_idx}; injected {_steers_applied} "
                    "steering message(s)"
                )
                break
            # (2026-11-30) The barrier is LAUNCHED as a task instead of awaited inline.
            # The old code ran the screen call to completion before touching the rest of
            # the batch, so one slow screenshot or goto stalled every later call - a 30s
            # browser navigation blocked a grep, a file read, a job start. The barrier
            # still waits for everything launched BEFORE it (order preserved above), and
            # calls AFTER it are independent by definition (files, search, web, python,
            # subagents, job controls - none touch the screen or the shared browser), so
            # they may overlap. Two screen calls in one batch stay sequential: the next
            # barrier flushes this one, and tool_lock is re-entrant per owner so the lock
            # alone would NOT have serialised them.
            # ONE shared flag list - the task sets it, the flush reads it.
            _started = [False]
            inflight.append((tc_idx, _started, asyncio.create_task(
                _guarded(tc, _started), name=f"tool-{tool_name[:20]}")))
            continue

        _started = [False]
        inflight.append((tc_idx, _started, asyncio.create_task(
            _guarded(tc, _started), name=f"tool-{tool_name[:20]}")))

    if await _flush():
        # A stop or a steer was observed WHILE waiting on launched calls. Started calls
        # already have their real results; nothing further is launched. A steer must be
        # injected here so the user's new instruction reaches the model before the next
        # provider call, not only later.
        if check_stop_event(config) is not None:
            logger.warning("[AGENTIC.DEBUG] react_loop - STOP EVENT observed while flushing "
                           "the batch; the turn ends after the flushed results")
            return agent_lessons
        if _has_pending_steers(config):
            _steers_applied = await _apply_pending_steers(config, messages)
            logger.warning(
                f"[AGENTIC.DEBUG] react_loop - steering observed while flushing the batch; "
                f"injected {_steers_applied} steering message(s)"
            )
    return agent_lessons


def _get_tool_compilation_error():
    """Get the ToolCompilationError class (lazy import, resolved ONCE).

    (2026-11 review fix B8) The old version re-ran the import on EVERY exception,
    so a hot error path paid two failed imports per occurrence. The class is a
    constant: resolve it once and cache the result (including the "not available"
    result), so repeated calls are a dict-free attribute read.
    """
    global _TOOL_COMPILATION_ERROR, _TOOL_COMPILATION_ERROR_RESOLVED
    if _TOOL_COMPILATION_ERROR_RESOLVED:
        return _TOOL_COMPILATION_ERROR
    try:
        from tools.remote_tools.dynamic_loader import ToolCompilationError
        _TOOL_COMPILATION_ERROR = ToolCompilationError
    except ImportError:
        # Fallback for when running in CLIENT context where module path differs
        try:
            from CLIENT.tools.remote_tools.dynamic_loader import ToolCompilationError
            _TOOL_COMPILATION_ERROR = ToolCompilationError
        except ImportError:
            _TOOL_COMPILATION_ERROR = None
    _TOOL_COMPILATION_ERROR_RESOLVED = True
    return _TOOL_COMPILATION_ERROR


async def run_react_loop(
    config: Any,
    messages: List[Dict],
    tool_executor: Any,
    conversation_history: List[Dict],
) -> str:
    """
    Run the ReAct loop for agentic mode.

    Args:
        config: AgenticModeConfig with provider, websocket, etc.
        messages: Initial messages list (system + history + user).
        tool_executor: ToolExecutor instance for tool execution.
        conversation_history: Original full conversation history.

    Returns:
        Final AI response string (includes all accumulated thinking steps).

    HISTORY TRIMMING CONTRACT (atomic units):
        - messages[0] (system prompt) is NEVER removed.
        - The CURRENT USER PROMPT (tagged `_protect` by build_messages) is NEVER
          removed - it must remain in the list at all times.
        - Everything else is grouped into atomic units (a turn = user msg + the AI
          reply that answered it + ALL its tool results) and only whole units are
          ever removed, oldest first. An assistant message with `tool_calls` can
          therefore never be separated from its tool results.
        - There is NO minimum-message floor: if the budget requires it, every
          removable unit goes and only [system prompt + current user prompt]
          remain. Per-message token counts are cached on the dicts by
          estimate_message_tokens(), so trimming is a cheap sum of ints.
    """
    func_start = time.time()

    # (2026-11 cycle history) The turn's recorder, when the chat bus attached one. Every
    # recorder call goes through _rec() so a dead recorder can never break the loop.
    cycle = getattr(config, "cycle", None)

    async def _rec(coro) -> None:
        """Await one recorder coroutine, swallowing every failure (history is best-effort)."""
        if coro is None:
            return
        try:
            await coro
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[AGENTIC.DEBUG] react_loop - cycle recorder failed: {e}")

    # (2026-11 interjection rule) True while a mid-run QUESTION (not a stop request) was
    # injected and its answer has not been settled yet. A text-only reply ends the turn, so
    # the first one written while this is set is DEFERRED: the job stays open until the
    # original task is finished.
    interjection_pending = False

    final_response = ""
    ai_response = ""
    tool_iteration = 0

    # LEARNED LESSONS - skipped on purpose (see _LESSONS_INJECTION_ENABLED at
    # module top). Kept as dead code: will be fixed later, we'll come back.
    agent_lessons = "" if not _LESSONS_INJECTION_ENABLED else None


    # TEMP THINKING ARRAY - holds partial answers (truncation resumes) until the final
    # text-only reply; ALL parts are then saved to history together.
    temp_thinking: List[str] = []

    # Consecutive replies with neither text nor tool calls (bounded by MAX_EMPTY_REPLIES).
    empty_replies = 0

    # REACT LOOP STATS - for UI display
    loop_tools = 0         # Total loop calls in this agentic run
    loop_no_tools = 0         # Non-tool calls since last tool call

    # (2026-08-23 session stats) TOTAL tokens generated by the provider across ALL loops of
    # this agentic run (loop 1 -> current). Pushed to the UI with every token_stats frame so
    # the status bar can show cumulative output for the whole session, not just one call.
    session_generated_tokens = 0

    # LAST PROVIDER STATS of this run - kept so the FINAL turn-stats line (spliced above
    # 'Agentic AI: TASK DONE' in the answer + sent as a 'turn_stats' frame) can show exactly
    # the numbers the UI status bar showed live during the loop.
    last_stats = None

    # CONTEXT-RECOVERY BUDGET - how many times this run may recover from a
    # truncated tool-call JSON (server error marker: "truncated its own tool-call").
    # Each recovery aggressively re-trims history and tells the model to use
    # smaller payloads. Bounded so a pathological prompt cannot loop forever.
    context_recoveries = 0
    MAX_CONTEXT_RECOVERIES = 3

    # TRUNCATION-RESUME BUDGET (2026-09-10): how many times this run may ask the model to
    # resume a response that was cut off at the output token limit (finish_reason == "length").
    # Before this fix a truncated answer looked like an ordinary thinking step, so the loop
    # hit max-thinking-iterations, injected 'end with TASK DONE' and re-asked from scratch -
    # forever (see plan_20260910_1835.md). After the budget is spent we accept the best
    # partial answer instead of looping.
    truncation_resumes = 0
    MAX_TRUNCATION_RESUMES = 2

    # NOTICE RE-OPEN BUDGET (2026-11-30): a runtime notice arriving during a final answer
    # re-opens the turn so the model acts on the new evidence. Without a bound a steady
    # notice stream (children settling one after another) re-opens the turn forever: each
    # re-open costs a provider call and burns AGENTIC_MAX_TOOL_ITERATIONS (2000) with no
    # progress. After the budget is spent the answer is accepted; any notice still queued
    # is delivered by the notice lane (a follow-up turn, itself budgeted by
    # NOTICE_MAX_CONSECUTIVE_WAKES), so nothing is lost.
    notice_reopens = 0

    # CONTEXT-GUARDIAN BUDGET - how many times this run may inject the near-limit
    # guardian message (fires at AGENTIC_TOKEN_WARNING_THRESHOLD of token_limit).
    # The AI is told to finish its current work step and dump everything into a plan
    # file so the next work loop can pick up where this one left off.
    token_warning_sent = False  # console warning fires once per run at the safe-margin threshold
    context_guardian_injections = 0

    # SERVER-TRUTH TOKEN ANCHOR (2026-08-19 fix): after each provider call we remember the
    # REAL prompt_tokens reported by llama.cpp via the server's token_stats frame. That is
    # the authoritative "how much of the context window does the CURRENT message list cost"
    # number - it includes system + history + tool schema exactly as sent. The client-side
    # chars/4 estimate (get_token_count) can overcount by 10-30x on large payloads, which is
    # what made the guardian fire at "1061%" while the server showed ~33% used and then wiped
    # 90+ healthy messages. We trust the server number once we have one; the local estimate
    # is only a fallback for iterations before any response has come back yet.
    server_prompt_tokens = None      # real prompt_tokens from last provider call (None until first)
    messages_len_at_last_stats = 0   # len(messages) when we last received stats — tracks which msgs are "new"
    est_drift_logged = False         # log the local-vs-server estimate divergence once per run

    token_limit = max_history_tokens()

    # Output room is guaranteed SERVER-side now: streaming.py clamps
    # max_tokens to (context window - real prompt estimate), so llama.cpp can
    # never run out of room mid tool-call JSON. The client's only job here is
    # keeping the whole message list under token_limit via atomic-unit trim.

    # Tool schema is sent to the model EVERY request but get_token_count() does not
    # count it. Include it in the trim math so we stop drifting past the real window.
    _tools_for_est = getattr(config, "ollama_tools", None) or []
    est_tools_tokens = sum(len(json.dumps(t)) for t in _tools_for_est) // 4 if _tools_for_est else 0


    try:
        while tool_iteration < AGENTIC_MAX_TOOL_ITERATIONS:
            iteration_start = time.time()
            logger.info("-" * 50)

            # Cycle history (2026-11): every provider call is ONE reasoning step of this
            # turn. begin_step() flushes the PREVIOUS step's AI text first (so a step's
            # speech never leaks into the next group) and returns the new step number; the
            # step is settled after the provider stats arrive, so the row carries real counters.
            step_no = tool_iteration + 1
            if cycle is not None:
                try:
                    step_no = await cycle.begin_step()
                except Exception:
                    pass  # history is best-effort


            # Check for stop signal at START of each iteration
            stopped = check_stop_event(config)
            if stopped:
                logger.warning("[AGENTIC.DEBUG] react_loop - STOP EVENT DETECTED, breaking loop")
                final_response = stopped
                if config.websocket:
                    await config.websocket.send_text(json.dumps({
                        "type": "system",
                        "content": "Generation stopped. Type new message to continue."
                    }))
                break




            # ---- LIVE STEERING (2026-10-23): drain queued user messages FIRST ----
            # A steer that arrived while the previous provider call / tool batch was
            # running becomes the newest user turn for THIS provider call.
            _steers_applied = await _apply_pending_steers(config, messages)
            if _steers_applied:
                logger.info(f"[AGENTIC.DEBUG] react_loop - injected {_steers_applied} live "
                            f"steering message(s) before provider call")
                # INTERJECTION RULE (2026-11): a pure mid-run QUESTION does not end the job.
                # Tell the model plainly (in-context) that the task stayed open: a text-only
                # reply ends the turn, so it must not finish by answering only the question.
                # A batch that also contains an explicit stop is NOT treated this way.
                if _steers_applied.interjection and not _steers_applied.stop:
                    interjection_pending = True
                    messages.append({"role": "user", "content": interjection_resume_notice(
                        int(_steers_applied), int(_steers_applied))})
                    logger.info("[AGENTIC.DEBUG] react_loop - interjection recorded: the final "
                                "answer is DEFERRED until the original task finishes")

            # ---- RUNTIME NOTICES (2026-12): drain the runtime's queue ----
            # A child or a background job settled while the previous provider call / tool
            # batch was running. Injected as the newest message for THIS provider call.
            # Deliberately AFTER steering: the user's correction outranks runtime news, and
            # a notice never sets interjection_pending (it is information, not a request).
            _notices_applied = await _apply_pending_notices(config, messages)
            if _notices_applied:
                logger.info(f"[AGENTIC.DEBUG] react_loop - injected {_notices_applied} runtime "
                            f"notice(s) before provider call")

            # Increment total loop call counter
            loop_tools += 1
            # TOKEN BUDGET CHAIN (checked EVERY iteration, in this order):
            #   1. WARNING  - console warning when usage reaches the safe-margin
            #      percentage (AGENTIC_TOKEN_WARNING_THRESHOLD of token_limit).
            #   2. GUARDIAN - at that same threshold, inject a [SYSTEM NOTICE]
            #      telling the AI to finish its current work and write a plan file
            #      so we can continue in the next work loop (same pattern as the
            #      max-thinking-iterations force-completion guardian).
            #   3. TRIM     - when the budget is actually exceeded, remove oldest
            #      atomic units from the list sent to the provider.
            # SERVER-TRUTH ANCHOR (2026-08-19 fix): prefer the REAL prompt token count
            # llama.cpp reported for the last request we sent. The message list only grows
            # between iterations, so "server prompt_tokens + local estimate delta since that
            # call" is a faithful current-usage figure - and it already includes the tool
            # schema (the server counts exactly what was sent), which is why est_tools_tokens
            # must NOT be added on top of it. The local chars/4 estimate stays only as a
            # fallback for iterations before any response has come back yet, and drift from
            # the server number is logged once so estimator problems remain diagnosable.
            if server_prompt_tokens is not None:
                _new_msgs = messages[messages_len_at_last_stats:]
                current_tokens = server_prompt_tokens + sum(estimate_message_tokens(m) for m in _new_msgs)
                local_est_now = get_token_count(messages) + est_tools_tokens
                if not est_drift_logged and abs(local_est_now - current_tokens) > 5000:
                    est_drift_logged = True
                    logger.warning(
                        f"[AGENTIC.DEBUG] react_loop - TOKEN ESTIMATE DRIFT: local chars/4 estimate "
                        f"{local_est_now} vs server-truth {current_tokens} tokens. Budget decisions use the "
                        f"server number (authoritative) - this is why spurious 1061% guardian warnings are gone."
                    )
            else:
                current_tokens = get_token_count(messages) + est_tools_tokens

            # ---- STAGE 1+2: safe-margin warning + context guardian (95%) ----
            if current_tokens >= int(token_limit * AGENTIC_TOKEN_WARNING_THRESHOLD):
                pct = round(100.0 * current_tokens / token_limit) if token_limit else 0

                # Console warning - once per run, the moment we cross the threshold.
                if not token_warning_sent:
                    token_warning_sent = True
                    logger.warning(
                        f"[AGENTIC.DEBUG] react_loop - TOKEN SAFE-MARGIN WARNING: {current_tokens}/{token_limit} "
                        f"tokens ({pct}% of safe budget, threshold {round(100*AGENTIC_TOKEN_WARNING_THRESHOLD)}%). "
                        f"Trimming will engage as soon as the limit is exceeded."
                    )
                    if config.websocket:
                        await config.websocket.send_text(json.dumps({
                            "type": "system",
                            "content": (f"Context window at {pct}% of the safe token budget. "
                                        f"History will be trimmed automatically if it grows further.")
                        }))

                # Context guardian - same pattern as the max-thinking-iterations
                # force-completion message: tell the AI to wrap up and leave a plan.
                if context_guardian_injections < AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS:
                    context_guardian_injections += 1
                    logger.warning(
                        f"[AGENTIC.DEBUG] react_loop - CONTEXT GUARDIAN {context_guardian_injections}/{AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS}: "
                        f"{current_tokens}/{token_limit} tokens ({pct}%). Injecting finish-current-work + plan message."
                    )
                    context_guardian_msg = {
                        "role": "user",
                        "content": (
                            f"[SYSTEM NOTICE] The context window is at {pct}% of its safe token budget. "
                            "You MUST now finish the current work step and STOP calling tools. "
                            "Write a plan file (plan_<YYYYMMDD_HHMM>_continue.md in the working folder) that captures: "
                            "(1) what is already DONE, (2) the exact remaining steps to finish the task, and "
                            "(3) any context/decisions needed to continue in the next work loop. "
                            "Then end your response with 'Agentic AI: TASK DONE' so we can resume from that plan later."
                        )
                    }
                    messages.append(context_guardian_msg)
                    # Trim right away too - the guardian message itself costs tokens and
                    # we want maximum room left for the AI's wrap-up + plan file.
                    live_protected = [0]
                    for _pi in range(len(messages) - 1, -1, -1):
                        if messages[_pi].get("role") == "user":
                            live_protected.append(_pi)
                            break
                    messages = safe_trim_history(messages, max(1024, token_limit - est_tools_tokens), live_protected)

            # ---- STAGE 3: hard limit exceeded -> trim oldest atomic units ----
            if current_tokens > token_limit:
                logger.warning(f"[AGENTIC.DEBUG] react_loop - TOKEN LIMIT EXCEEDED! Trimming history...")
                # Recompute protected indices live on every trim: each previous trim rebuilt
                # `messages`, so any index computed earlier is stale after the first removal.
                # The `_protect` tag on the current user prompt is the primary guard;
                # this keeps index-based protection correct for repeated trims within one run.
                live_protected = [0]
                for _pi in range(len(messages) - 1, -1, -1):
                    if messages[_pi].get("role") == "user":
                        live_protected.append(_pi)
                        break
                # Trim budget for the MESSAGE LIST ONLY: subtract the tool-schema cost so
                # (messages + tools) actually fits under token_limit. The old code passed
                # token_limit itself, leaving messages ~est_tools_tokens over budget - the
                # real prompt then hit the window with almost no output room left and
                # llama.cpp truncated its own tool-call JSON mid-string ("Failed to parse
                # tool call arguments" 500). See plan_20260817_trace_fix.md.
                messages = safe_trim_history(messages, max(1024, token_limit - est_tools_tokens), live_protected)
                after_trim_tokens = get_token_count(messages) + est_tools_tokens
                logger.debug(f"[AGENTIC.DEBUG] react_loop - After trim: {len(messages)} messages, {after_trim_tokens} tokens (incl. tool schema)")


            # LEARNED LESSONS - injection skipped on purpose (dead code kept for a
            # later fix; see _LESSONS_INJECTION_ENABLED at module top). The block
            # below stays exactly as it was so we can re-enable by flipping the flag.
            updated_messages = messages
            if agent_lessons and _LESSONS_INJECTION_ENABLED:
                logger.debug("[AGENTIC.DEBUG] react_loop - Injecting agent lessons into system prompt")
                for msg in updated_messages:
                    if msg["role"] == "system":
                        msg["content"] = f"""{msg["content"]}


{agent_lessons}"""
                        break

            # Provider call with automatic restart on connection error.
            # Bounded recovery: if the SERVER reports its own tool-call JSON was truncated
            # (marker: "truncated its own tool-call"), re-trim history aggressively and tell
            # the model to split payloads - instead of letting one context exhaustion kill
            # a multi-hour agentic run. The trim-budget fix above makes this rare; this is
            # the safety net for when it still happens (e.g. protected messages alone are huge).
            try:
                provider_result, stats = await call_provider_with_retry(config, updated_messages)


            except RuntimeError as _prov_err:
                if "truncated its own tool-call" in str(_prov_err) and context_recoveries < MAX_CONTEXT_RECOVERIES:
                    context_recoveries += 1
                    logger.warning(
                        f"[AGENTIC.DEBUG] react_loop - CONTEXT RECOVERY {context_recoveries}/{MAX_CONTEXT_RECOVERIES}: "
                        f"server truncated its own tool-call JSON. Aggressively re-trimming history..."                    )
                    live_protected = [0]
                    for _pi in range(len(messages) - 1, -1, -1):
                        if messages[_pi].get("role") == "user":
                            live_protected.append(_pi)
                            break
                    aggressive_limit = max(1024, int(token_limit * 0.75) - est_tools_tokens)
                    messages = safe_trim_history(messages, aggressive_limit, live_protected)
                    recovery_notice = {"role": "user", "content": (
                        "[SYSTEM NOTICE] Your previous tool call was truncated because the context window ran out. "
                        "Continue the task with SMALLER payloads: split large code into multiple small calls "
                        "(one file per call, < ~200 lines each) and avoid re-sending content you already have.")
                    }
                    messages.append(recovery_notice)
                    continue
                raise
            except Exception as _cancel_err:
                # CANCEL FIX (2026-09-07): the user clicked Stop WHILE this provider call was
                # streaming. direct_mode/relay_mode raise UserCancelled in that case (matched by
                # class name - it is defined per module and crosses the retry boundary as a plain
                # exception). Treat it exactly like the loop-start stop check: no error log, no
                # re-raise, just break with the clean "stopped" result. Before this fix the
                # exception fell into the generic handler below ("UNEXPECTED ERROR") and bubbled
                # up as a provider failure even though it was a deliberate user cancel.
                if type(_cancel_err).__name__ == "UserCancelled":
                    logger.warning("[AGENTIC.DEBUG] react_loop - UserCancelled raised by provider mid-stream; stopping cleanly without executing further tools")
                    final_response = "Generation stopped by user."
                    if config.websocket:
                        try:
                            await config.websocket.send_text(json.dumps({
                                "type": "system",
                                "content": "Generation stopped. Type new message to continue."
                            }))
                        except Exception:
                            pass  # UI socket may already be closed after a stop click
                    break
                # Not a user cancel - this was a real error: re-raise so the normal
                # handlers below (truncated-tool-call recovery / generic error path) apply.
                raise _cancel_err


            # Push token stats to UI
            logger.debug("[AGENTIC.DEBUG] react_loop - Sending token stats to UI")
            if stats and getattr(stats, "generated_tokens", 0):
                session_generated_tokens += int(stats.generated_tokens or 0)
            _sess_stats = build_session_stats(config)   # active-time elapsed (pauses excluded) + last turn
            loop_stats = {
                "loop_tools": loop_tools,
                "loop_no_tools": loop_no_tools,
                # (2026-08-23 session stats) additive UI fields:
                "session_generated_tokens": session_generated_tokens,
                "session_elapsed_sec": _sess_stats["session_elapsed_sec"],
                "last_session_sec": _sess_stats["last_session_sec"],
            }
            await push_token_stats(config.websocket, stats, loop_stats)
            if stats is not None:
                last_stats = stats   # remember for the final turn-stats line at loop exit

            if stats:
                logger.info(
                    f"prompt={stats.prompt_tokens}, "
                    f"generated={stats.generated_tokens}, "
                    f"speed={stats.calculated_speed}, "
                    f"remaining={stats.tokens_remaining}"
                )

                # Cycle history: settle THIS reasoning step with the real provider counters.
                await _rec(cycle.settle_step(stats, time.time() - iteration_start) if cycle else None)

                # Refresh the SERVER-TRUTH anchor: this is the real cost of exactly the
                # message list we just sent (system + history + tool schema, as counted by
                # llama.cpp). We remember len(messages) at that moment so the next iteration
                # can slice messages[len_at_last:] and only estimate tokens for NEW messages.
                if stats.prompt_tokens and stats.prompt_tokens > 0:
                    server_prompt_tokens = int(stats.prompt_tokens)
                    messages_len_at_last_stats = len(messages)




            # Check for tool calls — FILTER OUT MALFORMED ONES (empty/whitespace names)
            raw_tool_calls = _extract_tool_calls(provider_result)
            tool_calls = [tc for tc in raw_tool_calls if _is_valid_tool_call(tc)]


            if tool_calls:

                # CANCEL FIX (2026-09-07): the stop event can be set WHILE the provider call was
                # in flight (user clicked Stop mid-stream). The response still arrives with tool
                # calls, but executing them after a cancel is exactly the "processes keep running"
                # bug. Check again right before execution: if we were cancelled, do NOT run any of
                # these tools - break out and report stop instead.
                stopped = check_stop_event(config)
                if stopped:
                    logger.warning("[AGENTIC.DEBUG] react_loop - STOP EVENT set during provider call; discarding %d pending tool call(s), not executing", len(tool_calls))
                    final_response = stopped
                    if config.websocket:
                        try:
                            await config.websocket.send_text(json.dumps({
                                "type": "system",
                                "content": "Generation stopped. Type new message to continue."
                            }))
                        except Exception:
                            pass  # UI socket may already be closed after a stop click
                    break

                # TOOL EXECUTION - clear temp thinking array (new action phase starts)
                logger.debug(f"[AGENTIC.DEBUG] react_loop - Tool execution detected, clearing {len(temp_thinking)} accumulated thinking step(s)")
                temp_thinking = []

                # Tool work is REAL continued work on the original task, so the
                # interjection deferral window closes here.
                interjection_pending = False
                empty_replies = 0


                # Reset loop_no_tools counter on tool use (for UI stats)
                loop_no_tools = 0

                logger.debug(f"[AGENTIC.DEBUG] react_loop - Processing {len(tool_calls)} native tool call(s) (filtered from {len(raw_tool_calls)})")


                # Add assistant message with tool calls.
                # content must be None (not an empty string) - the standard OpenAI format for
                # pure tool-call messages; an empty string can confuse chat templates and is one
                # of the triggers for malformed follow-up output from llama.cpp.
                assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls
                }
                messages.append(assistant_msg)


                # Execute the batch (2026-12): independent tools run concurrently,
                # screen/browser tools act as order barriers. Every tool_call id gets
                # exactly one role="tool" message, in the model's original order.
                # CANCEL / STEER mid-batch: launched calls are flushed (their side effects
                # already happened) and the remaining ones get explicit NOT EXECUTED text.
                agent_lessons = await _execute_tool_batch(
                    tool_calls, tool_executor, config, messages,
                    conversation_history, agent_lessons,
                )


                tool_iteration += 1


                iteration_elapsed = time.time() - iteration_start
                continue
            else:
                # No tool calls - check if task is truly done
                ai_response = provider_result if isinstance(provider_result, str) else str(provider_result)

                # (2026-10-11) The model can copy a past turn's spliced "Loop stats:" line
                # out of its own history into this answer. Strip it BEFORE accumulation so
                # the persisted bubble carries exactly ONE stats line - the one the loop
                # splices at exit below (build_messages() keeps old lines from the model's
                # view; this is the safety net for anything that still slips through).
                ai_response = strip_turn_stats_lines(ai_response)

                # ---- LIVE STEERING vs. premature completion (2026-10-23) ----
                # The user typed a correction while this (possibly final) answer was
                # streaming. Do NOT accept the task as done: keep what was streamed as an
                # assistant turn, inject the steering message AFTER it, and let the model
                # revise/continue. This is what makes steering during a long final answer
                # feel live instead of "too late - the task already finished".
                if _has_pending_steers(config):
                    if ai_response:
                        if messages and messages[-1].get("role") == "assistant":
                            messages[-1]["content"] = (messages[-1].get("content") or "") + "\n\n" + ai_response
                            invalidate_message_tokens(messages[-1])
                        else:
                            messages.append({"role": "assistant", "content": ai_response})
                    _steers_applied = await _apply_pending_steers(config, messages)
                    if _steers_applied:
                        # The streamed answer stays in `messages` (the model must see what it
                        # already wrote) but is NOT accumulated into temp_thinking: it was a
                        # premature completion, superseded by the steering message. Keeping it
                        # would persist a stale final answer ahead of the revised one.
                        logger.warning(
                            f"[AGENTIC.DEBUG] react_loop - completion deferred: injected "
                            f"{_steers_applied} steering message(s) during the final answer"
                        )
                        # Same interjection rule as the loop-head boundary: a pure question
                        # must not end the job when the model answers it with text only.
                        if _steers_applied.interjection and not _steers_applied.stop:
                            interjection_pending = True
                            messages.append({"role": "user", "content": interjection_resume_notice(
                                int(_steers_applied), int(_steers_applied))})
                        loop_no_tools = 0
                        tool_iteration += 1
                        continue

                # ---- RUNTIME NOTICE vs. completion (2026-12) ----
                # A child or background job settled while this (possibly final) answer was
                # streaming. New evidence, not a request: the answer stays in context but is
                # NOT accepted as done - the turn re-opens so the model acts on the notice.
                # This is the notice counterpart of the steering deferral, and it does NOT
                # set interjection_pending (a notice never asks the model to answer first).
                if _has_pending_notices(config):
                    if ai_response:
                        if messages and messages[-1].get("role") == "assistant":
                            messages[-1]["content"] = (messages[-1].get("content") or "") + "\n\n" + ai_response
                            invalidate_message_tokens(messages[-1])
                        else:
                            messages.append({"role": "assistant", "content": ai_response})
                    _notices_applied = await _apply_pending_notices(config, messages)
                    if _notices_applied:
                        if notice_reopens < MAX_NOTICE_REOPENS:
                            notice_reopens += 1
                            logger.warning(
                                f"[AGENTIC.DEBUG] react_loop - completion re-opened: injected "
                                f"{_notices_applied} runtime notice(s) during the final answer "
                                f"({notice_reopens}/{MAX_NOTICE_REOPENS})"
                            )
                            loop_no_tools = 0
                            tool_iteration += 1
                            continue
                        # Budget spent: accept the answer. The notice(s) were still injected
                        # and persisted, so the evidence is in history and the next turn sees
                        # it - nothing is lost, but the loop stops re-opening forever.
                        logger.warning(
                            f"[AGENTIC.DEBUG] react_loop - notice re-open budget spent "
                            f"({MAX_NOTICE_REOPENS}): accepting the answer; "
                            f"{_notices_applied} notice(s) stay in history for the next turn"
                        )
                        if config.websocket:
                            try:
                                await config.websocket.send_text(json.dumps({
                                    "type": "system",
                                    "content": (f"{_notices_applied} background result(s) arrived "
                                                "while this answer was finishing - they are recorded "
                                                "in the turn history and will be picked up next."),
                                }))
                            except Exception:
                                pass  # UI gone - the notice is still persisted

                # TRUNCATION FIX (2026-09-10): the server reports finish_reason == "length" via
                # stats.truncated. A cut-off answer must be resumed, not accepted as final -
                # otherwise the user gets half an answer. Bounded by MAX_TRUNCATION_RESUMES;
                # after the budget is spent we accept the best partial instead of looping.
                if stats and getattr(stats, "truncated", False):
                    if truncation_resumes < MAX_TRUNCATION_RESUMES:
                        truncation_resumes += 1
                        logger.warning(
                            f"[AGENTIC.DEBUG] react_loop - TRUNCATED RESPONSE {truncation_resumes}/{MAX_TRUNCATION_RESUMES}: "
                            f"provider hit its output limit ({stats.generated_tokens} tokens) mid-answer. Asking for a resume."
                        )
                        if config.websocket:
                            try:
                                await config.websocket.send_text(json.dumps({
                                    "type": "system",
                                    "content": ("Response was cut off at the output limit - "
                                                 f"asking the model to resume ({truncation_resumes}/{MAX_TRUNCATION_RESUMES})..."),
                                }))
                            except Exception:
                                pass  # UI socket may already be closed
                        # The model can only resume if it SEES what it already wrote - add the partial
                        # answer to the message list first. None-safe: a pure tool-call assistant
                        # message carries content=None, so use the (content or "") merge rule.
                        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
                            messages[-1]["content"] = (messages[-1].get("content") or "") + "\n\n" + ai_response
                            invalidate_message_tokens(messages[-1])  # content mutated in place -> refresh cached token count
                        else:
                            messages.append({"role": "assistant", "content": ai_response})

                        resume_msg = {
                            "role": "user",
                            "content": (
                                "[SYSTEM NOTICE] Your previous response was CUT OFF mid-sentence by the output token limit. "
                                "Continue EXACTLY where you stopped - do NOT repeat or re-describe anything already written, "
                                "and finish the answer now as plain text."
                            ),
                        }
                        messages.append(resume_msg)

                        # Keep the partial in temp_thinking: final_response is built from ALL
                        # parts, so both halves of the split answer end up in history.
                        # (This does NOT count as a thinking iteration for the max-thinking counter.)
                        temp_thinking.append(ai_response)

                        tool_iteration += 1
                        continue
                    else:
                        # Resume budget spent: accept the best partial answer instead of looping forever.
                        logger.warning(
                            f"[AGENTIC.DEBUG] react_loop - TRUNCATION RESUME BUDGET SPENT ({MAX_TRUNCATION_RESUMES}): "
                            f"accepting partial answer as final instead of looping."
                        )
                        if config.websocket:
                            try:
                                await config.websocket.send_text(json.dumps({
                                    "type": "system",
                                    "content": ("Model kept hitting the output limit - delivering the best partial answer. "
                                                 "The response may be incomplete."),
                                }))
                            except Exception:
                                pass
                        temp_thinking.append(ai_response)
                        separator = "\n\n" + "=" * 50 + "\n\n"
                        final_response = separator.join(temp_thinking) if len(temp_thinking) > 1 else ai_response
                        # Keep the history contract: final answers end with the TASK DONE marker.
                        if "Agentic AI: TASK DONE" not in final_response:
                            final_response += "\n\nAgentic AI: TASK DONE"
                        # The answer row still opens - this IS the delivered answer.
                        await _rec(cycle.announce_final(step_no) if cycle else None)
                        break

                if not ai_response.strip():
                    # Neither text nor tool calls: a provider hiccup. Ask again a bounded
                    # number of times, then deliver an honest fallback instead of spinning.
                    empty_replies += 1
                    if empty_replies <= MAX_EMPTY_REPLIES:
                        logger.warning(
                            f"[AGENTIC.DEBUG] react_loop - EMPTY RESPONSE {empty_replies}/{MAX_EMPTY_REPLIES}: "
                            "provider returned neither text nor tool calls; asking again."
                        )
                        messages.append({
                            "role": "user",
                            "content": (
                                "[SYSTEM NOTICE] Your previous response was empty (no text and no tool "
                                "calls). If the task is finished, give your final answer as plain text "
                                "now; otherwise call the next tool."
                            ),
                        })
                        tool_iteration += 1
                        continue
                    logger.warning(
                        f"[AGENTIC.DEBUG] react_loop - EMPTY RESPONSE BUDGET SPENT ({MAX_EMPTY_REPLIES}): "
                        "delivering fallback answer."
                    )
                    ai_response = "The model returned an empty response and could not continue this task."

                # INTERJECTION RULE (2026-11): a mid-run QUESTION was injected but the
                # ORIGINAL task is not finished, so this text-only reply (the answer to the
                # question) is a premature completion. Keep the answer in context (the model
                # must see what it wrote), push the job back with an explicit continue
                # instruction, and do NOT persist it as done. A batch that also asked to stop
                # is honoured above (interjection is only set for pure questions).
                if interjection_pending:
                    interjection_pending = False
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["content"] = (messages[-1].get("content") or "") + "\n\n" + ai_response
                        invalidate_message_tokens(messages[-1])
                    else:
                        messages.append({"role": "assistant", "content": ai_response})
                    messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM NOTICE] You answered the mid-run message, but that was NOT a stop "
                            "request. Do NOT end the task here - continue the ORIGINAL task from where "
                            "you left it with tool calls, and give your final text answer only when "
                            "that task is genuinely finished."
                        ),
                    })
                    logger.warning(
                        "[AGENTIC.DEBUG] react_loop - final answer DEFERRED: the task stayed open after "
                        "a mid-run question (not a stop request)"
                    )
                    loop_no_tools = 0
                    if config.websocket:
                        try:
                            await config.websocket.send_text(json.dumps({
                                "type": "system",
                                "content": interjection_resume_notice(1, 1),
                            }))
                        except Exception:
                            pass  # UI gone - the deferral still happened
                    await _rec(cycle.feedback(interjection_resume_notice(1, 1)) if cycle else None)
                    tool_iteration += 1
                    continue

                # NATIVE COMPLETION (2026-11-09): a text-only reply IS the final answer -
                # no marker required from the model. Tool-call replies continue the loop;
                # anything else ends the turn here.
                temp_thinking.append(ai_response)

                # Cycle history: open the answer row. The UI needs this signal because a
                # reply to a steer and the real answer are identical content frames.
                await _rec(cycle.announce_final(step_no) if cycle else None)

                logger.info(f"[AGENTIC.DEBUG] react_loop - Final text answer accepted ({len(temp_thinking)} part(s))")

                # Concatenate all parts into final_response for history preservation
                separator = "\n\n" + "=" * 50 + "\n\n"
                final_response = separator.join(temp_thinking)
                # UI/history continuity: the backend appends the legacy marker itself so the
                # stats splice, the visible answer and old history stay exactly as before -
                # the model no longer has to generate it.
                if "Agentic AI: TASK DONE" not in final_response:
                    final_response = final_response.rstrip("\n") + "\n\nAgentic AI: TASK DONE"
                break


        # (2026-10-10) FINAL TURN STATS: splice the last loop-stats line directly above
        # "Agentic AI: TASK DONE" in the persisted answer AND send it as a "turn_stats"
        # frame so the live bubble gets the same line. The UI handler is idempotent, so
        # replayed frames and DB-loaded bubbles can never double-print it.
        if final_response and "Agentic AI: TASK DONE" in final_response:
            _elapsed_final = time.time() - func_start
            stats_line = _format_turn_stats_line(last_stats, loop_tools, loop_no_tools,
                                                 session_generated_tokens, _elapsed_final)
            if stats_line:
                # (2026-10-10 fix) Target ONLY the marker on its own line. The model can
                # mention "Agentic AI: TASK DONE" mid-prose ("...end with 'Agentic AI:
                # TASK DONE'") - splicing before such a meta-mention put the stats at the
                # TOP of the answer, which looked humanly wrong. The LAST standalone line
                # carrying the marker is the real end marker; if none exists (marker only
                # appears mid-line), append both lines cleanly at the very end instead.
                _m = None
                for _m in re.finditer(r"(?m)^[ \t]*Agentic AI: TASK DONE[ \t]*$", final_response):
                    pass  # loop ends with the LAST standalone match (or None)
                if _m is not None:
                    final_response = (final_response[:_m.start()] + stats_line + "\n"
                                      + final_response[_m.start():])
                else:
                    final_response = (final_response.rstrip("\n") + "\n" + stats_line
                                      + "\nAgentic AI: TASK DONE")
                logger.info(f"[AGENTIC.DEBUG] react_loop - Final turn stats: {stats_line}")
                if config.websocket:
                    try:
                        await config.websocket.send_text(json.dumps({
                            "type": "turn_stats",
                            "line": stats_line,
                        }))
                    except Exception:
                        pass  # UI socket may already be closed - the persisted answer still carries it

        # Loop exit - RESTORED to match working version
        if not final_response:
            logger.warning("[AGENTIC.DEBUG] react_loop - Loop exited but no final_response, using fallback")


            # If we have accumulated thinking but loop ended without TASK DONE marker, save it anyway
            if temp_thinking:
                separator = "\n\n" + "=" * 50 + "\n\n"
                final_response = separator.join(temp_thinking)
                logger.info(f"[AGENTIC.DEBUG] react_loop - Saving {len(temp_thinking)} thinking step(s) from incomplete loop")
            else:
                final_response = ai_response if ai_response else "I've completed the tasks."
                # Keep the history contract: final answers end with the TASK DONE marker
                # (the model may have been cut off before writing it).
                if "Agentic AI: TASK DONE" not in final_response:
                    final_response += "\n\nAgentic AI: TASK DONE"


        # Final token usage log
        final_tokens = get_token_count(messages)
        total_time = time.time() - func_start


        logger.info("=" * 60)
        logger.debug(f"[AGENTIC.DEBUG] react_loop - Final token count: {final_tokens} / {token_limit} ({len(messages)} messages)")
        logger.debug(f"[AGENTIC.DEBUG] react_loop - Total execution time: {total_time:.2f}s")
        logger.debug(f"[AGENTIC.DEBUG] react_loop - Final response length: {len(final_response)} chars")
        logger.debug(f"[AGENTIC.DEBUG] react_loop - EXIT")
        logger.info("=" * 60)


        return final_response


    except Exception as e:
        elapsed = time.time() - func_start
    
        # CRITICAL FIX: Check for ToolCompilationError BEFORE catching generic RuntimeError.
        # ToolCompilationError inherits from RuntimeError and was being caught by the 
        # (ConnectionError, TimeoutError, RuntimeError, FileNotFoundError) tuple below,
        # which caused it to be logged as "PROVIDER ERROR" - completely hiding the real
        # compilation failure and triggering useless retries.
        tool_comp_error = _get_tool_compilation_error()
        if tool_comp_error and isinstance(e, tool_comp_error):
            logger.error(
                f"[AGENTIC.DEBUG] react_loop - TOOL COMPILATION ERROR (non-retryable) "
                f"after {elapsed:.2f}s | iteration={tool_iteration}: {e}"
            )
        elif isinstance(e, (ConnectionError, TimeoutError)):
            logger.error(f"[AGENTIC.DEBUG] react_loop - PROVIDER ERROR after {elapsed:.2f}s | iteration={tool_iteration}")
        elif isinstance(e, FileNotFoundError):
            logger.error(f"[AGENTIC.DEBUG] react_loop - FILE NOT FOUND ERROR after {elapsed:.2f}s | iteration={tool_iteration}: {e}")
        else:
            logger.error(f"[AGENTIC.DEBUG] react_loop - UNEXPECTED ERROR ({type(e).__name__}) after {elapsed:.2f}s | iteration={tool_iteration}: {e}")


        # Even on error, log accumulated thinking if any
        if temp_thinking:
            logger.warning(f"[AGENTIC.DEBUG] react_loop - Error occurred with {len(temp_thinking)} thinking step(s) in buffer")


        raise


def _format_turn_stats_line(last_stats: Any, loop_tools: int, loop_no_tools: int,
                            session_generated_tokens: int, elapsed_sec: float) -> str:
    """Format the FINAL turn-stats line for an agentic run (2026-10-10).

    Renders EXACTLY like the UI status bar ("Loop stats: ... | 177K ctx | 9.3K sent |
    167.4K left | \u26a1 20.6 tok/s | \U0001F4DD 0.1K gen | \u23f1 0:08") so the user sees in the
    answer the same statistics that were live during the thinking process. Spliced directly
    above "Agentic AI: TASK DONE" and sent as a "turn_stats" frame for the live bubble.

    Returns "" when there is no provider stats object yet (nothing to report).
    """
    if last_stats is None:
        return ""
    try:
        window = int(getattr(last_stats, "context_window", 0) or 0)
        sent = int(getattr(last_stats, "prompt_tokens", 0) or 0)
        remaining = getattr(last_stats, "tokens_remaining", None)
        if remaining is None or remaining < 0:
            remaining = max(0, window - sent - (int(getattr(last_stats, "generated_tokens", 0) or 0)))
        speed = getattr(last_stats, "calculated_speed", None)
        if not speed and session_generated_tokens > 0 and elapsed_sec > 0:
            speed = session_generated_tokens / elapsed_sec   # honest fallback over the whole turn

        parts = [f"Loop stats: {loop_tools} / {loop_no_tools}",
                 f"{window // 1024}K ctx",
                 f"{sent / 1024:.1f}K sent",
                 f"{remaining / 1024:.1f}K left"]
        if speed and speed > 0:
            parts.append(f"\u26a1 {speed:.1f} tok/s")
        if session_generated_tokens > 0:
            parts.append(f"\U0001F4DD {session_generated_tokens / 1024:.1f}K gen")
        total = max(0.0, float(elapsed_sec or 0))
        h, rem = divmod(int(total), 3600)
        m, s = divmod(rem, 60)
        parts.append("\u23f1 " + (f"{h}:{m:02d}:" if h else f"{m}:") + f"{s:02d}")
        return " | ".join(parts)
    except Exception:
        logger.debug("[AGENTIC.DEBUG] turn-stats line formatting failed", exc_info=True)
        return ""


def _is_valid_tool_call(tc: Dict) -> bool:
    """Validate a tool call has a non-empty function name.
    
    Filters out malformed tool calls where the model returned empty/whitespace names.
    This prevents 'unknown' tool errors when the model hallucinates incomplete tool calls.
    
    Tool call structure from llama.cpp streaming:
        {
            "id": "call_xxx",
            "type": "function",
            "function": {"name": "tool_name", "arguments": "{}"}
        }
    """
    if not isinstance(tc, dict):
        return False
    
    func = tc.get("function")
    if not isinstance(func, dict):
        return False
    
    name = func.get("name", "")
    if not isinstance(name, str) or not name.strip():
        logger.warning(f"[AGENTIC.DEBUG] react_loop - Filtered out malformed tool call with empty/whitespace name: {tc}")
        return False
    
    return True


def _extract_tool_calls(result: Any) -> list:
    """Extract tool calls from provider result."""
    if isinstance(result, dict) and result.get("type") == "tool_calls":
        return result["tool_calls"]


    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict) and parsed.get("type") == "tool_calls":
                return parsed["tool_calls"]
        except json.JSONDecodeError:
            logger.debug("[AGENTIC.DEBUG] react_loop - Response is plain text (not JSON), no tool calls")


    return []


def _repair_json_arguments(args_str):
        """Repair and parse malformed JSON from streamed tool call arguments.

        Only ONE safe completion is attempted: appending missing closing brackets
        when the payload is otherwise complete. A genuinely TRUNCATED payload (output
        token limit hit mid-string) or a corrupted one (raw newlines / stray quotes in
        values) is NEVER "repaired" - any dict salvaged from it would be partial data,
        and executing that (e.g. half-written python_exec code) is worse than failing loud.
        Returns parsed dict on success, None when the payload cannot be recovered
        safely (caller then feeds an actionable error back to the model).
        """
        if not args_str or not isinstance(args_str, str):
            return None

        # First try: parse as-is
        try:
            result = json.loads(args_str)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, TypeError):
            logger.debug("React loop: initial JSON parse failed for tool args, attempting repair")

        s = args_str.strip()

        # Safe completion: append the missing closing bracket when the payload is
        # otherwise complete (streaming occasionally drops only the final closer).
        for closing in ['}', ']', '{}', ']']:
            try:
                repaired = json.loads(s + closing)
                if isinstance(repaired, dict):
                    return repaired
            except (json.JSONDecodeError, TypeError):
                continue




        logger.error(f"[AGENTIC.DEBUG] react_loop - Could not repair JSON arguments (length={len(args_str)}): {args_str[:200]}")
        return None
