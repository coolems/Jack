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


import json
import logging
import re
import time
from typing import List, Dict, Any, Tuple

from config import (
    AGENTIC_MAX_TOOL_ITERATIONS,
    AGENTIC_MAX_THINKING_ITERATIONS,
    AGENTIC_TOKEN_WARNING_THRESHOLD,
    AGENTIC_CONTEXT_GUARDIAN_WINDOW_FRACTION,
    AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS,
    get_context_window,
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
# lessons text counted into message_budget (it rides in the system message, so
# it must be measured like the tool schema) before flipping the flag.
# ============================================================
_LESSONS_INJECTION_ENABLED = False


from .base_mode import push_token_stats, check_stop_event, build_session_stats, strip_turn_stats_lines
from .provider_call import call_provider_with_retry
from .tool_lock import tool_lock, SCREEN_BROWSER_TOOLS
# (2026-11-07) Extensive context-budget diagnostics: every limit stage logs WHO holds
# the tokens (per-role histogram, unremovable block, injected notices, per-tool schema
# cost, top contributors) instead of a single percentage line.
from .context_report import build_budget_report, log_budget


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
# A mid-run question must never close the job, so a completion marker written right after
# answering one is DEFERRED (see _interjection_pending handling below) unless the same batch
# also asked for a stop.
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


def _loop_boundary(messages: List[Dict]) -> int:
    """Index where the CURRENT loop begins: one past the protected user prompt.

    An index captured at loop entry goes stale as soon as a trim rebuilds the list
    (removals happen BEFORE the boundary, so every later index shifts down). The
    boundary is therefore derived from the `_protect` tag that rides on the message
    dict itself. Everything after that prompt is what this loop appended - assistant
    replies, tool results, mid-run steering and notices - and is never trimmed.
    """
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("_protect") is True:
            return i + 1
    return len(messages)


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


def _append_skipped_tool_results(messages: List[Dict], tool_calls: List[Dict], start_idx: int) -> int:
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
            "content": TOOL_SKIPPED_FOR_STEER,
            "tool_call_id": tool_id,
        })
        skipped += 1
    return skipped


# Lazy-import ToolCompilationError to avoid circular dependency at module load time
_TOOL_COMPILATION_ERROR: Any = None
_TOOL_COMPILATION_ERROR_RESOLVED = False


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
        - THE CURRENT LOOP IS NEVER REMOVED. `_loop_boundary(messages)` is the boundary:
          boundary: everything appended after build_messages() - this loop's
          assistant replies, tool_calls, tool results and mid-run steering - is the
          working context and stays available for the whole run. Trimming eats only
          the PREVIOUS turns of the session.
        - Everything removable is grouped into atomic units (a turn = user msg + the
          AI reply that answered it + ALL its tool results) and only whole units are
          ever removed, oldest first. An assistant message with `tool_calls` can
          therefore never be separated from its tool results.
        - When the loop alone cannot fit the budget, the trim is a no-op and the
          context guardian fires: the AI writes plan_<...>_continue.md (in .temp/plans/) and stops, so
          the NEXT loop resumes from user input + AI output + that plan. Only after
          the guardian has spoken may a trim shrink the loop (allow_loop_trim=True),
          to give the wrap-up answer room. Per-message token counts are cached on the
          dicts by estimate_message_tokens(), so trimming is a cheap sum of ints.
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
    # injected and its answer has not been settled yet. A completion marker written while
    # this is set is DEFERRED: the job stays open until the original task is finished.
    interjection_pending = False

    final_response = ""
    ai_response = ""
    tool_iteration = 0

    # LEARNED LESSONS - skipped on purpose (see _LESSONS_INJECTION_ENABLED at
    # module top). Kept as dead code: will be fixed later, we'll come back.
    agent_lessons = "" if not _LESSONS_INJECTION_ENABLED else None


    # TEMP THINKING ARRAY - accumulates AI reasoning responses between tool calls
    # When TASK DONE is encountered, ALL thinking steps are saved to history together
    temp_thinking: List[str] = []


    # CONSECUTIVE THINKING COUNTER - tracks thinking-only iterations without tool use
    consecutive_thinking_count = 0

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

    # CONTEXT-GUARDIAN BUDGET - how many times this run may inject the near-limit
    # guardian message. It fires at AGENTIC_CONTEXT_GUARDIAN_WINDOW_FRACTION of the FULL
    # window AND only when trimming could not free any room (see stage 3 below).
    # The AI is told to finish its current work step and dump everything into a plan
    # file so the next work loop can pick up where this one left off.
    #
    # ARMING (2026-11-07 fix): the guardian fires on a CROSSING, not on a state. It is
    # armed while usage sits below the threshold and disarmed the moment it injects, so a
    # run cannot get a second "stop calling tools" notice 40s later while still sitting at
    # the same percentage (the old code injected 1/3 then 2/3 back-to-back with no new
    # information). A trim that brings usage back below the threshold re-arms it, so a
    # genuinely re-grown context can still get a fresh notice.
    token_warning_sent = False  # console warning fires once per run at the safe-margin threshold
    context_guardian_injections = 0
    guardian_armed = True

    # SERVER-TRUTH TOKEN ANCHOR (2026-08-19 fix, OVERHEAD form 2026-11-07): after each
    # provider call we remember the REAL prompt_tokens llama.cpp reported for exactly the
    # list we sent, and store it as an OFFSET against our own estimate:
    #     anchor_overhead = server prompt_tokens - local estimate of that same list
    #     current_tokens  = anchor_overhead + local estimate of the CURRENT list
    # The chat-template markup the server counts but our chars/4 estimate does not is the
    # overhead, so the server number stays authoritative while the local estimate still
    # tracks every change - including a TRIM, which the old index-slice form could not
    # represent (it kept the anchor high forever and re-counted messages the server had
    # already counted, which is what made the guardian claim 96% while the UI showed
    # 101K tokens free).
    anchor_overhead = 0          # server prompt_tokens - local estimate at anchor time
    anchor_set = False           # True once a real prompt_tokens has been received
    est_drift_logged = False     # log a suspiciously large estimate gap once per run
    stats_without_prompt_logged = False  # log "server sent no prompt_tokens" once per run

    # PREVIOUS ITERATION'S COST (2026-11-07 diagnostics). The guardian log used to report
    # only a percentage, so a run stuck at the limit could not say what grew or what could
    # not be trimmed. Every budget stage now logs the full report (see logic/context_report.py)
    # and this value makes the per-iteration JUMP visible.
    prev_tokens = None

    token_limit = max_history_tokens()

    # FULL context window as reported by the SERVER - the same number the UI status bar
    # shows ("200K ctx"). The guardian threshold is measured against THIS, not against the
    # 0.90 history budget, so a notice can never claim "nearly full" while the bar shows
    # ~100K tokens still free. Read live: the SERVER rewrites CONTEXT_WINDOW_TOKENS on
    # every auth handshake.
    window_tokens = get_context_window() or token_limit

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
            # running becomes the newest user turn for THIS provider call. Reset the
            # thinking counter: fresh user input is not the model "stalling".
            _steers_applied = await _apply_pending_steers(config, messages)
            if _steers_applied:
                consecutive_thinking_count = 0
                logger.info(f"[AGENTIC.DEBUG] react_loop - injected {_steers_applied} live "
                            f"steering message(s) before provider call")
                # INTERJECTION RULE (2026-11): a pure mid-run QUESTION does not end the job.
                # Tell the model plainly (in-context) that the task stayed open, so a later
                # 'TASK DONE' cannot be justified by having answered only the question. A
                # batch that also contains an explicit stop is NOT treated this way.
                if _steers_applied.interjection and not _steers_applied.stop:
                    interjection_pending = True
                    messages.append({"role": "user", "content": interjection_resume_notice(
                        int(_steers_applied), int(_steers_applied))})
                    logger.info("[AGENTIC.DEBUG] react_loop - interjection recorded: completion "
                                "marker is DEFERRED until the original task finishes")

            # Increment total loop call counter
            loop_tools += 1
            # TOKEN BUDGET CHAIN (checked EVERY iteration, in this order):
            #   1. WARNING  - console/UI warning when usage reaches the safe-margin
            #      percentage of the MESSAGE-LIST budget (AGENTIC_TOKEN_WARNING_THRESHOLD
            #      of token_limit) - the moment trimming is about to engage.
            #   2. TRIM     - when the budget is actually exceeded, remove oldest
            #      atomic units from the list sent to the provider.
            #   3. GUARDIAN - only when trimming could NOT free room (nothing removable, or
            #      still over budget afterwards) AND usage sits at AGENTIC_CONTEXT_GUARDIAN_
            #      WINDOW_FRACTION of the FULL context window: inject a [SYSTEM NOTICE]
            #      telling the AI to finish its current work and write a plan file so we can
            #      continue in the next work loop. One notice per crossing; a trim that
            #      lowers usage re-arms it.
            # BUDGET EXPRESSION (2026-11 review fix A7) - defined ONCE per iteration and
            # used by every stage below, so the check and the trim can never disagree:
            #   current_tokens   = TOTAL prompt cost in SERVER units (local estimate of the
            #                      list + tool schema + the server overhead). Compared against
            #                      warn_limit / guardian_limit / token_limit by stages 1/2/3.
            #   message_budget   = token_limit - est_tools_tokens = what the MESSAGE LIST
            #                      alone may hold. Every safe_trim_history() call uses this,
            #                      never the raw token_limit.
            #   trim_budget      = message_budget - anchor_overhead = the same budget expressed
            #                      in LOCAL-estimate units (safe_trim_history counts with our
            #                      estimator). Without this subtraction a trim that "fits"
            #                      locally still overflows the real window whenever the server
            #                      counts more than chars/4 - the old 1061% failure mode.
            # Deriving all three here (instead of re-spelling the arithmetic at each stage) is
            # what keeps the schema counted exactly once: the overhead is measured WITH the
            # schema included, and the trim budget subtracts it exactly once from the limit.
            message_budget = max(1024, token_limit - est_tools_tokens)
            local_est_now = get_token_count(messages) + est_tools_tokens
            current_tokens = local_est_now + anchor_overhead
            trim_budget = max(1024, min(token_limit, message_budget - anchor_overhead))
            # guardian_limit is derived HERE (not inside stage 2) so stage 1 can quote the
            # guardian margin in its report and every stage measures the same number.
            guardian_limit = int(window_tokens * AGENTIC_CONTEXT_GUARDIAN_WINDOW_FRACTION)

            # ROUTINE TRACE (2026-11-07): one line per iteration so the growth curve is
            # visible in the normal log (INFO - the COOLEMS logger is INFO by default, so a
            # DEBUG line would never be written). Cheap: no message scanning, just ints.
            logger.info(
                f"[CONTEXT-BUDGET] iter={tool_iteration} msgs={len(messages)} "
                f"cost={current_tokens} (local {local_est_now} + overhead {anchor_overhead} + schema {est_tools_tokens}) "
                f"| history_budget={token_limit} guardian_limit={guardian_limit} trim_budget={trim_budget} "
                f"| window={window_tokens} left={max(0, window_tokens - current_tokens)} "
                f"delta={current_tokens - (prev_tokens or 0)}"
            )

            if anchor_set:
                if not est_drift_logged and abs(current_tokens - local_est_now) > 20000:
                    est_drift_logged = True
                    logger.warning(
                        f"[AGENTIC.DEBUG] react_loop - TOKEN ESTIMATE DRIFT: server-truth "
                        f"{current_tokens} vs local chars/4 estimate {local_est_now} tokens "
                        f"(overhead {anchor_overhead}). Budget decisions use the server number "
                        f"(authoritative); the overhead is chat-template markup, not message text."
                    )

            # ---- STAGE 1: safe-margin console/UI warning ----
            # Fires once per run when the MESSAGE-LIST budget (max_history_tokens, i.e. 0.90
            # of the window) is nearly full - that is the moment trimming is about to engage.
            warn_limit = int(token_limit * AGENTIC_TOKEN_WARNING_THRESHOLD)
            if current_tokens >= warn_limit and not token_warning_sent:
                token_warning_sent = True
                pct_hist = round(100.0 * current_tokens / token_limit) if token_limit else 0
                logger.warning(
                    f"[AGENTIC.DEBUG] react_loop - TOKEN SAFE-MARGIN WARNING: {current_tokens}/{token_limit} "
                    f"history-budget tokens ({pct_hist}% of the budget, threshold "
                    f"{round(100 * AGENTIC_TOKEN_WARNING_THRESHOLD)}%), {window_tokens} total window. "
                    f"Trimming engages as soon as the budget is exceeded."
                )
                # Full state dump (2026-11-07): which messages hold the tokens, what is
                # unremovable, how much the schema costs, how far from the guardian limit.
                log_budget(logger, "safe-margin-warning", build_budget_report(
                    "safe-margin-warning", window_tokens=window_tokens, token_limit=token_limit,
                    message_budget=message_budget, trim_budget=trim_budget, warn_limit=warn_limit,
                    guardian_limit=guardian_limit, local_est=local_est_now,
                    current_tokens=current_tokens, anchor_overhead=anchor_overhead,
                    anchor_set=anchor_set, est_tools_tokens=est_tools_tokens,
                    prev_tokens=prev_tokens, messages=messages, tools=_tools_for_est,
                    guardian_injections=context_guardian_injections,
                    guardian_max=AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS,
                    guardian_armed=guardian_armed, token_warning_sent=token_warning_sent))
                if config.websocket:
                    await config.websocket.send_text(json.dumps({
                        "type": "system",
                        "content": (f"History budget at {pct_hist}% ({current_tokens} sent, "
                                    f"{max(0, window_tokens - current_tokens)} left of the "
                                    f"{window_tokens} window). History will be trimmed automatically "
                                    f"if it grows further.")
                    }))

            # ---- STAGE 2: hard limit exceeded -> trim oldest atomic units ----
            # ORDER FIX (2026-11-07): the trim runs BEFORE the guardian. The old order
            # injected "context nearly full, STOP calling tools" while the trimmer still had
            # plenty of removable history - so a run that could have continued by trimming got
            # told to wrap up instead. The guardian now only speaks when trimming CANNOT help.
            #
            # ROOT CAUSE of the reported "guardian fires immediately / lots of empty window":
            # the committed code measured the guardian against token_limit
            # (= max_history_tokens() = 0.90 x window) and quoted that as a percentage OF the
            # budget, so it fired at 176561/184320 = "96%" while the UI status bar showed the
            # SERVER's real window (204800) with thousands of tokens still free. The notice
            # contradicted the UI and stopped productive work early.
            stuck_at_limit = False
            trim_removed = 0
            before_trim_tokens = 0   # set by stage 2; the trim log quotes before -> after
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
                # trim_budget additionally subtracts the server overhead, so the trimmed list
                # fits in SERVER units too - a trim that only "fits" in chars/4 units is what
                # previously kept the real prompt over the window.
                before_trim_tokens = current_tokens
                # LOOP RULE (2026-11-08): trim only what sits BEFORE the current loop.
                # The boundary is derived live from the `_protect` tag, so it stays correct
                # after every rebuild the previous trim performed.
                messages = safe_trim_history(
                    messages, trim_budget, live_protected,
                    loop_start=_loop_boundary(messages),
                )
                # Post-trim figure uses the SAME expression as the decision (local estimate +
                # server overhead): the overhead form survives a rebuild of the list, so the
                # number logged here is the real current cost, not a stale anchor. The trim
                # also re-arms the guardian when usage drops back under its threshold.
                current_tokens = get_token_count(messages) + est_tools_tokens + anchor_overhead
                trim_removed = before_trim_tokens - current_tokens
                if current_tokens < guardian_limit:
                    guardian_armed = True
                logger.warning(
                    f"[AGENTIC.DEBUG] react_loop - After trim: {len(messages)} messages, "
                    f"{before_trim_tokens} -> {current_tokens} tokens "
                    f"(server overhead + local est, incl. tool schema), removed={trim_removed} tok "
                    f"({round(100.0 * trim_removed / before_trim_tokens) if before_trim_tokens else 0}% of context), "
                    f"message_budget={message_budget}, trim_budget={trim_budget}, "
                    f"guardian_limit={guardian_limit}"
                )
                log_budget(logger, "hard-trim", build_budget_report(
                    "hard-trim", window_tokens=window_tokens, token_limit=token_limit,
                    message_budget=message_budget, trim_budget=trim_budget, warn_limit=warn_limit,
                    guardian_limit=guardian_limit,
                    local_est=get_token_count(messages) + est_tools_tokens,
                    current_tokens=current_tokens, anchor_overhead=anchor_overhead,
                    anchor_set=anchor_set, est_tools_tokens=est_tools_tokens,
                    prev_tokens=prev_tokens, messages=messages, tools=_tools_for_est,
                    guardian_injections=context_guardian_injections,
                    guardian_max=AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS,
                    guardian_armed=guardian_armed, token_warning_sent=token_warning_sent))
                # STUCK LIMIT (2026-11-07, extended 2026-11-08 by the LOOP RULE): a trim that
                # removed nothing means the budget is held entirely by messages the trimmer may
                # never delete - system prompt + protected current user prompt + the CURRENT
                # LOOP. Say it plainly: history trimming cannot help in that state, so the
                # context guardian must ask for a continuation plan instead.
                if current_tokens > token_limit:
                    stuck_at_limit = True
                    logger.warning(
                        f"[AGENTIC.DEBUG] react_loop - TRIM NO-OP: still {current_tokens} > {token_limit} "
                        f"after removing every removable unit OUTSIDE the current loop "
                        f"(loop messages={max(0, len(messages) - _loop_boundary(messages))}). "
                        f"The cost sits in UNREMOVABLE messages "
                        f"(see 'unremovable total' in the report above) plus tool schema "
                        f"({est_tools_tokens}) and server overhead ({anchor_overhead})."
                    )
                prev_tokens = current_tokens

            # ---- STAGE 3: context guardian ----
            # Fires when trimming CANNOT help this run: stuck_at_limit means the trim removed
            # nothing, which with the LOOP RULE (2026-11-08) is exactly the state the user
            # described - the budget is held by UNREMOVABLE material (system prompt + protected
            # current user prompt + the CURRENT LOOP, which is the AI's working context and is
            # never trimmed). The only correct response is a continuation plan: finish the work
            # step, write plan_<...>_continue.md (in .temp/plans/), stop; the NEXT loop resumes from user input +
            # AI output + that plan.
            # A trim that discards half the context is NOT a stop condition: with the LOOP RULE
            # it can only eat PREVIOUS turns, which is legitimate trimming - the trim log already
            # shows the size of the pass (removed=N tok, before -> after).
            # Percentages are quoted against the FULL window the UI shows, never against the
            # 0.90 budget, so the notice and the status bar can never disagree.
            if guardian_armed and stuck_at_limit:
                pct = round(100.0 * current_tokens / window_tokens) if window_tokens else 0
                left = max(0, window_tokens - current_tokens)
                loop_msgs = max(0, len(messages) - _loop_boundary(messages))
                reason = ("history trimming can no longer free room - the cost sits in "
                          "UNREMOVABLE material: system prompt + the current user prompt + "
                          f"the CURRENT LOOP ({loop_msgs} messages), which is your working "
                          "context and is never trimmed")

                # Context guardian - same pattern as the max-thinking-iterations
                # force-completion message: tell the AI to wrap up and leave a plan.
                if context_guardian_injections < AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS:
                    context_guardian_injections += 1
                    guardian_armed = False   # crossing consumed - re-arms only after a trim drops usage back
                    logger.warning(
                        f"[AGENTIC.DEBUG] react_loop - CONTEXT GUARDIAN {context_guardian_injections}/{AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS}: "
                        f"{current_tokens}/{window_tokens} context tokens ({pct}% of window), {left} left, "
                        f"history_budget={token_limit}, trim_removed={trim_removed} tok, stuck={stuck_at_limit}. "
                        f"Reason: {reason}. Injecting finish-current-work + plan message."
                    )
                    # EXTENSIVE OBSERVATION (2026-11-07): the full picture at the moment the
                    # guardian fires - limits, cost breakdown, the jump that crossed the
                    # threshold, the unremovable block, injected notices, per-tool schema cost
                    # and the most expensive messages. This is what answers "where did we hit
                    # the big limit".
                    log_budget(logger, "context-guardian-fire", build_budget_report(
                        "context-guardian-fire", window_tokens=window_tokens, token_limit=token_limit,
                        message_budget=message_budget, trim_budget=trim_budget, warn_limit=warn_limit,
                        guardian_limit=guardian_limit, local_est=local_est_now,
                        current_tokens=current_tokens, anchor_overhead=anchor_overhead,
                        anchor_set=anchor_set, est_tools_tokens=est_tools_tokens,
                        prev_tokens=prev_tokens, messages=messages, tools=_tools_for_est,
                        guardian_injections=context_guardian_injections,
                        guardian_max=AGENTIC_MAX_CONTEXT_GUARDIAN_INJECTIONS,
                        guardian_armed=False, token_warning_sent=token_warning_sent))
                    context_guardian_msg = {
                        "role": "user",
                        "content": (
                            f"[SYSTEM NOTICE] The context window is at {pct}% ({current_tokens} tokens sent, "
                            f"{left} left of {window_tokens}) and {reason}. "
                            "You MUST now finish the current work step and STOP calling tools. "
                            "Write a plan file (plan_<YYYYMMDD_HHMM>_continue.md - write_file with path='' puts it in .temp/plans/) that captures: "
                            "(1) what is already DONE, (2) the exact remaining steps to finish the task, and "
                            "(3) any context/decisions needed to continue in the next work loop. "
                            "Then end your response with 'Agentic AI: TASK DONE' so we can resume from that plan later."
                        )
                    }
                    messages.append(context_guardian_msg)

            # ---- STAGE 4: post-guardian last-resort trim (loop may shrink ONLY now) ----
            # The guardian notice itself costs tokens, and with the LOOP RULE nothing removable
            # is left, so the wrap-up call can exceed the FULL server window - which would make
            # llama.cpp truncate the answer (and the plan file the AI is about to write). Since a
            # continue plan has now been asked for, shrinking the loop is safe: remove the OLDEST
            # loop units first, so the newest work - what the plan must describe - survives.
            # This never runs before the guardian has spoken.
            if context_guardian_injections > 0 and current_tokens > window_tokens:
                logger.warning(
                    f"[AGENTIC.DEBUG] react_loop - POST-GUARDIAN TRIM: {current_tokens} > "
                    f"{window_tokens} full window. Guardian already fired (plan requested), so the "
                    f"current loop may be shrunk - oldest loop units first, newest kept."
                )
                live_protected = [0]
                for _pi in range(len(messages) - 1, -1, -1):
                    if messages[_pi].get("role") == "user":
                        live_protected.append(_pi)
                        break
                messages = safe_trim_history(
                    messages, trim_budget, live_protected,
                    loop_start=_loop_boundary(messages), allow_loop_trim=True,
                )
                current_tokens = get_token_count(messages) + est_tools_tokens + anchor_overhead
                if current_tokens < guardian_limit:
                    guardian_armed = True
                logger.warning(
                    f"[AGENTIC.DEBUG] react_loop - After post-guardian trim: {len(messages)} messages, "
                    f"{current_tokens} tokens (window {window_tokens}), loop messages now "
                    f"{max(0, len(messages) - _loop_boundary(messages))}"
                )

            # Remember this iteration's cost so the NEXT iteration's report shows the growth
            # (the tool results + replies added since) instead of always comparing to zero.
            prev_tokens = current_tokens

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
                    # Same LOCAL-estimate units as the other trims: 0.75 of the history budget
                    # minus the tool schema AND the server overhead, so the aggressive trim
                    # really leaves room in what llama.cpp counts. LOOP RULE (2026-11-08): it
                    # may shrink the CURRENT LOOP only after the guardian already spoke (a
                    # continue plan exists) - otherwise it eats previous turns only.
                    aggressive_limit = max(1024, int(token_limit * 0.75) - est_tools_tokens - anchor_overhead)
                    messages = safe_trim_history(
                        messages, aggressive_limit, live_protected,
                        loop_start=_loop_boundary(messages),
                        allow_loop_trim=context_guardian_injections > 0,
                    )
                    # Same re-arm rule as the other trims: the aggressive trim lowered the
                    # real cost, so the guardian must not stay armed/disarmed on a stale figure.
                    current_tokens = get_token_count(messages) + est_tools_tokens + anchor_overhead
                    if current_tokens < guardian_limit:
                        guardian_armed = True
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

                # Refresh the SERVER-TRUTH anchor as an OVERHEAD: server prompt_tokens minus
                # our estimate of exactly the list we just sent (system + history + tool
                # schema). That difference is the chat-template markup the server counts and
                # chars/4 does not; adding it to the estimate of the CURRENT list gives a
                # faithful figure that also goes DOWN when history is trimmed.
                #
                # A missing/zero prompt_tokens is a real bug signal, not a no-op: silently
                # skipping the anchor left the previous overhead in place forever, so the
                # guardian kept counting messages the server had already counted. Log it once
                # per run so the provider-side cause stays diagnosable.
                prompt_tokens = int(getattr(stats, "prompt_tokens", 0) or 0)
                if prompt_tokens > 0:
                    anchor_overhead = prompt_tokens - (get_token_count(updated_messages) + est_tools_tokens)
                    anchor_set = True
                elif not stats_without_prompt_logged:
                    stats_without_prompt_logged = True
                    logger.warning(
                        "[AGENTIC.DEBUG] react_loop - Provider stats carried NO prompt_tokens - "
                        "the server-truth anchor cannot be refreshed; budget decisions fall back "
                        "to the local chars/4 estimate. Check the provider's usage/usage-per-field "
                        "reporting (this is what previously inflated the context-guardian count)."
                    )




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


                # Reset consecutive thinking counter on tool use
                consecutive_thinking_count = 0


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


                # Execute each tool call and accumulate lessons.
                # CANCEL FIX (2026-09-07): re-check the stop event between individual tools of a
                # multi-tool batch so a cancel mid-batch stops the remaining ones too.
                for tc_idx, tc in enumerate(tool_calls):
                    if check_stop_event(config) is not None:
                        logger.warning("[AGENTIC.DEBUG] react_loop - STOP EVENT set mid-batch; skipping %d remaining tool call(s)", len(tool_calls) - tc_idx)
                        break

                    # ---- LIVE STEERING pre-empts pending side effects (2026-10-23) ----
                    # The user redirected the task WHILE this tool batch was pending. Do not
                    # run actions they may no longer want; complete the batch with explicit
                    # "not executed" tool results (the message contract requires one result
                    # per tool_call id) and inject the steering message before the next call.
                    if _has_pending_steers(config):
                        _skipped = _append_skipped_tool_results(messages, tool_calls, tc_idx)
                        _steers_applied = await _apply_pending_steers(config, messages)
                        # NO "Steering applied" system notice (2026-11): the skipped calls
                        # already carry their own "NOT EXECUTED" tool result, so a duplicate
                        # banner in the chat is noise. The log line stays for diagnosis.
                        logger.warning(
                            f"[AGENTIC.DEBUG] react_loop - steering pre-empted {_skipped} pending "
                            f"tool call(s) at index {tc_idx}; injected {_steers_applied} steering message(s)"
                        )
                        break

                    agent_lessons = await _execute_single_tool_call(
                        tc, tool_executor, config, messages,
                        conversation_history, agent_lessons
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
                        # would persist a stale "TASK DONE" answer ahead of the revised one.
                        logger.warning(
                            f"[AGENTIC.DEBUG] react_loop - completion deferred: injected "
                            f"{_steers_applied} steering message(s) during the final answer"
                        )
                        consecutive_thinking_count = 0
                        loop_no_tools = 0
                        tool_iteration += 1
                        continue

                if "Agentic AI: TASK DONE" in ai_response:
                    # INTERJECTION RULE (2026-11): a mid-run QUESTION was answered but the
                    # ORIGINAL task is not finished, so this marker is a premature completion.
                    # Keep the answer in context (the model must see what it wrote), push the
                    # job back with an explicit continue instruction, and do NOT persist it as
                    # done. A batch that also asked to stop is honoured above (interjection is
                    # only set for pure questions).
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
                                "you left it and only write 'Agentic AI: TASK DONE' when that task is "
                                "genuinely finished."
                            ),
                        })
                        logger.warning(
                            "[AGENTIC.DEBUG] react_loop - completion DEFERRED: the task stayed open after "
                            "a mid-run question (not a stop request)"
                        )
                        consecutive_thinking_count = 0
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

                    # TASK COMPLETE - build final response from ALL accumulated thinking + this last message
                    temp_thinking.append(ai_response)

                    # Cycle history: open the answer row. The UI needs this signal because a
                    # reply to a steer and the real answer are identical content frames.
                    await _rec(cycle.announce_final(step_no) if cycle else None)


                    if len(temp_thinking) > 1:
                        logger.info(f"[AGENTIC.DEBUG] react_loop - TASK DONE with {len(temp_thinking)} accumulated thinking step(s)")
                    else:
                        logger.debug("[AGENTIC.DEBUG] react_loop - TASK DONE (single response)")


                    # Concatenate all thinking steps into final_response for history preservation
                    separator = "\n\n" + "=" * 50 + "\n\n"
                    final_response = separator.join(temp_thinking)
                    break
                else:
                    # TRUNCATION FIX (2026-09-10): the server now reports finish_reason == "length" via
                    # stats.truncated. A cut-off answer must NOT be treated as a normal thinking step -
                    # that is what caused the infinite 'Max thinking iterations' re-ask loop.
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
                            # answer to the message list first (same merge rule as the normal thinking path).
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
                                    "and finish the answer now. End your response with 'Agentic AI: TASK DONE'."
                                ),
                            }
                            messages.append(resume_msg)

                            # Keep the partial in temp_thinking: at TASK DONE time final_response is built from
                            # ALL accumulated steps, so both halves of the split answer end up in history.
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

                    # Increment consecutive thinking counter
                    consecutive_thinking_count += 1

                    # INTERJECTION RULE (2026-11): a marker-free turn after a mid-run question
                    # is REAL continued work, so the deferral window closes - the next
                    # 'TASK DONE' is a genuine completion of the original task.
                    if interjection_pending:
                        interjection_pending = False
                        logger.debug("[AGENTIC.DEBUG] react_loop - interjection settled: the model "
                                     "continued the original task (marker-free turn)")


                    # Increment loop_no_tools counter (for UI stats)
                    loop_no_tools += 1
                    logger.debug(f"[AGENTIC.DEBUG] react_loop - Consecutive thinking count: {consecutive_thinking_count}")


                    logger.debug("[AGENTIC.DEBUG] react_loop - No 'Agentic AI: TASK DONE' marker, accumulating thinking")


                    # Accumulate this thinking step in temp array
                    temp_thinking.append(ai_response)
                    logger.debug(f"[AGENTIC.DEBUG] react_loop - Thinking step {len(temp_thinking)} accumulated (total chars: {sum(len(t) for t in temp_thinking)})")


                    # Add to internal messages for context (merged with previous assistant if consecutive).
                    # None-safe: a pure tool-call assistant message carries content=None (OpenAI format),
                    # so `+=` would raise TypeError when one sits at messages[-1] - use the same
                    # `(content or "") + ...` merge rule as the truncation-resume path above.
                    # ORDER (2026-11-07 fix): the reply is appended BEFORE the max-thinking check below.
                    # The old order injected the force-completion notice and `continue`d without ever
                    # appending this reply, so the model never saw what it had already written (and the
                    # turn's own text vanished from history).
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["content"] = (messages[-1].get("content") or "") + "\n\n" + ai_response
                        invalidate_message_tokens(messages[-1])  # content mutated in place -> refresh cached token count
                    else:
                        assistant_msg = {
                            "role": "assistant",
                            "content": ai_response
                        }
                        messages.append(assistant_msg)

                    # Check if we have exceeded max thinking iterations without tool use
                    if consecutive_thinking_count >= AGENTIC_MAX_THINKING_ITERATIONS:
                        # The notice must name its OWN cause and state the context state, so a
                        # reader (and the model) can never mistake this for a context-guardian
                        # stop. (2026-11-07) With the limit at 1 this fired after a single prose
                        # turn between two tool batches and ended runs that still had ~133K
                        # tokens free - reported by the user as "context guardian stopped me".
                        logger.warning(
                            f"[AGENTIC.DEBUG] react_loop - Max thinking iterations ({AGENTIC_MAX_THINKING_ITERATIONS}) "
                            f"reached! Injecting completion message to AI. "
                            f"context={current_tokens}/{window_tokens} ({round(100.0 * current_tokens / window_tokens) if window_tokens else 0}% of window, "
                            f"{max(0, window_tokens - current_tokens)} left) - this is a THINKING-STALL limit, "
                            f"NOT a context limit (guardian fired {context_guardian_injections} time(s) this run)."
                        )

                        pct_now = round(100.0 * current_tokens / window_tokens) if window_tokens else 0
                        # Send a message forcing the AI to complete with TASK DONE marker
                        force_completion_msg = {
                            "role": "user",
                            "content": (
                                f"[SYSTEM NOTICE] You sent {consecutive_thinking_count} replies in a row with no tool "
                                f"call and no completion marker, so this run is treated as stalled. This is NOT a "
                                f"context limit - the window is at {pct_now}% ({current_tokens} of {window_tokens} tokens, "
                                f"{max(0, window_tokens - current_tokens)} still free). "
                                "If the task is finished: provide your final answer and end with "
                                "'Agentic AI: TASK DONE'. If it is NOT finished: do NOT claim completion - write a "
                                "plan file (plan_<YYYYMMDD_HHMM>_continue.md - write_file with path='' puts it in .temp/plans/) naming what is "
                                "done and the exact remaining steps, then end with 'Agentic AI: TASK DONE' so the "
                                "next run can resume from that plan."
                            )
                        }
                        messages.append(force_completion_msg)


                        # Reset counter so we do not keep injecting on next iteration
                        consecutive_thinking_count = 0


                        # Continue loop - let provider process the force message
                        tool_iteration += 1
                        continue

                    tool_iteration += 1
                    continue


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


async def _execute_single_tool_call(
    tc: Dict,
    tool_executor: Any,
    config: Any,
    messages: List[Dict],
    conversation_history: List[Dict],
    agent_lessons: str
) -> str:
    """Execute a single tool call and update messages with result.

    Tool call structure from llama.cpp streaming (OpenAI format):
        {
            "id": "call_xxx",
            "type": "function",
            "function": {"name": "tool_name", "arguments": "{}"}
        }

    Args:
        tc: Tool call dict from provider.
        tool_executor: ToolExecutor instance.
        config: AgenticModeConfig.
        messages: Current message list (modified in place).
        conversation_history: Full conversation history for context.
        agent_lessons: Existing lessons string to append to.

    Returns:
        Updated agent_lessons string.
    """
    # CRITICAL FIX: Tool call structure has name/arguments nested inside "function" key!
    func = tc.get("function", {}) if isinstance(tc, dict) else {}
    tool_name = func.get("name", "unknown") if isinstance(func, dict) else "unknown"
    tool_id = tc.get("id", "")


    # Parse arguments - handle both JSON strings and already-parsed dicts
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
        # The usual cause is the output token limit cutting the tool-call JSON mid-string,
        # so the fix on the next iteration is a smaller payload, not a retry of the same one.
        error_msg = (
            f"Tool argument parsing failed for '{tool_name}': malformed or truncated JSON in tool arguments "
            '(likely hit the output token limit). Do NOT retry the same large payload - re-issue this'
            ' call with a SMALLER argument (e.g. split the code into multiple smaller python_exec calls)'
        )

        # Add tool result message with error
        tool_result_msg = {
            "role": "tool",
            "content": error_msg,
            "tool_call_id": tool_id
        }
        messages.append(tool_result_msg)
        return agent_lessons


    try:
        # FIX: Call tool_executor.execute() (the correct method name), NOT execute_tool().
        # ToolExecutor.execute() returns Tuple[str, str] = (tool_result, agent_lessons)
        # CRITICAL FIX (2026-08-27): pass live_messages=messages. Tool-viewed image
        # pixels (view_image / screenshots) are sanitized OUT of the tool-result text
        # before it is appended to history; ToolExecutor re-attaches them to the
        # PROTECTED current-user prompt so the NEXT provider call carries real pixels.
        # Without live_messages it fell back to `conversation_history` - a DIFFERENT
        # list than the one actually sent to the provider (build_messages copies
        # history[:-1] and creates a fresh protected user message). The re-attach
        # silently landed on a dict that is never sent -> the model only ever saw the
        # "[image: ... chars sanitized]" text reference and answered from imagination.
        # (2026-09-08 multi-chat) Screen/browser tools are serialized across chats:
        # two agentic loops must not fight over the same mouse/keyboard at once.
        _owner = getattr(config, 'conversation_id', None) or '_unknown'
        if tool_name in SCREEN_BROWSER_TOOLS:
            async with tool_lock(_owner):
                result_text, new_lessons = await tool_executor.execute(
                    tool_name=tool_name,
                    tool_args=parsed_args,
                    conversation_history=conversation_history,
                    websocket=config.websocket,
                    live_messages=messages,
                    conversation_id=getattr(config, 'conversation_id', None)  # python_exec approval gate (2026-09-24)
                )
        else:
            result_text, new_lessons = await tool_executor.execute(
                tool_name=tool_name,
                tool_args=parsed_args,
                conversation_history=conversation_history,
                websocket=config.websocket,
                live_messages=messages,
                conversation_id=getattr(config, 'conversation_id', None)  # python_exec approval gate (2026-09-24)
            )

        # Merge any new lessons from the tool execution
        if new_lessons:
            agent_lessons = (agent_lessons + "\n\n" + new_lessons).strip() if agent_lessons else new_lessons


        # Add tool result message
        tool_result_msg = {
            "role": "tool",
            "content": str(result_text),
            "tool_call_id": tool_id
        }
        messages.append(tool_result_msg)


    except Exception as e:
        logger.error(f"[AGENTIC.DEBUG] react_loop - Tool execution failed for {tool_name}: {e}")
        error_msg = f"Tool execution error: {str(e)[:500]}"
    
        # Add tool result message with error
        tool_result_msg = {
            "role": "tool",
            "content": error_msg,
            "tool_call_id": tool_id
        }
        messages.append(tool_result_msg)


    return agent_lessons
