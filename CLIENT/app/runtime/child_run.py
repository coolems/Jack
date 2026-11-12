"""child_run — execute one delegated subagent turn (client side, 2026-12).

Two seeds, one loop:

  spawn  messages = [system] + [task]                        (standalone task)
  fork   messages = [system] + parent's COMPLETED turns + [task]

The in-flight turn is NEVER seeded: the parent's assistant reply for the current turn is
not in conversation_history yet (it is persisted after the turn), and copying the live
loop list would leak the parent's tool results into the child.

The child runs the SAME run_react_loop with its own ToolExecutor, whose schema is
filtered to CHILD_ALLOWED (screen/browser/image/delegation tools removed) and whose
execute() is fail-closed on anything forbidden. Its provider socket is ChildSink, so the
child's frames ride on the parent's channel tagged with child_id — the UI shows child
activity inside the parent's turn block, and a detached parent socket buffers them.

Consent still belongs to the HUMAN: a child's python_exec / URL / desktop call goes
through the parent channel's ExecApprovalState (same conversation id), so a child can
never run code on this machine without the user approving it.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from logic.agentic import AgenticModeConfig
from logic.base_mode import strip_turn_stats_from_messages, strip_turn_stats_lines
from logic.react_loop import run_react_loop
from logic.tool_executor import ToolExecutor
from app.runtime.children import CHILD_FORBIDDEN_TOOLS, ChildRecord, ChildSink

logger = logging.getLogger("COOLEMS.Runtime.ChildRun")

#: The history contract react_loop appends to every final answer. A child's own
#: conversation must NOT carry it: kept as-is, the next (continuation) provider call
#: sees a past "Agentic AI: TASK DONE" + stats line as few-shot and copies it into its
#: reply - the same self-imitation bug build_messages() guards against for the parent.
_TASK_DONE_MARKER = "Agentic AI: TASK DONE"


def _clean_child_answer(text: str) -> str:
    """The child's answer WITHOUT parent-UI noise: spliced stats line + TASK DONE marker.

    `sink.captured_text` is already clean (it is the streamed content); the fallback
    `result` is react_loop's final_response, which carries both. Used for the answer we
    append to the child's kept conversation, so a continuation never imitates them.
    """
    if not isinstance(text, str) or not text:
        return ""
    cleaned = strip_turn_stats_lines(text)
    if _TASK_DONE_MARKER in cleaned:
        cleaned = cleaned.replace(_TASK_DONE_MARKER, "")
    return cleaned.strip()


def _filter_schema(tools: List[Dict]) -> List[Dict]:
    """Child tool schema: drop every forbidden name (the schema is the promise)."""
    out = []
    for t in tools or []:
        name = ""
        if isinstance(t, dict):
            fn = t.get("function") or {}
            name = fn.get("name") or t.get("name") or ""
        if name and name not in CHILD_FORBIDDEN_TOOLS:
            out.append(t)
    return out


def _seed_messages(rec: ChildRecord, context: Dict, task: str,
                   continuation: str = "") -> List[Dict]:
    """Build the child's message list. Never raises; a bad seed degrades to spawn."""
    system = context.get("system_prompt") or ""
    messages: List[Dict] = [{"role": "system", "content": system}]

    if rec.mode == "fork" and not continuation:
        seed = [m for m in (context.get("history") or []) if isinstance(m, dict)]
        # Completed turns only. An assistant message carrying tool_calls (and the tool
        # results that answer it) belongs to the parent's IN-FLIGHT loop, not to the
        # conversation: seeding it would leak the parent's live tool results into the
        # child AND break the provider contract (a tool_call with no matching result).
        seed = [m for m in seed
                if m.get("role") != "tool" and not m.get("tool_calls")]
        strip_turn_stats_from_messages(seed)
        messages.extend(seed)

    if continuation:
        # Continuation: the child keeps its own conversation (rec.messages), so the
        # previous reply is already there; append the user's follow-up only.
        # (2026-11-30 fix) When rec.messages is EMPTY - the child produced no messages
        # (it errored before the loop, or a scripted/legacy executor never filled them) -
        # the continuation used to be sent with NO system prompt at all: the provider got
        # "user: follow up" and nothing else, so the child answered with no identity, no
        # tool promise and no subagent contract. Rebuild the seed instead.
        if rec.messages:
            messages = [dict(m) for m in rec.messages]
            if not any(m.get("role") == "system" for m in messages):
                messages.insert(0, {"role": "system", "content": system})
        else:
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": rec.task}]
        messages.append({"role": "user", "content": continuation, "_protect": True})
        return messages

    body = task if rec.mode == "fork" else (
        "You are a delegated subagent. Work autonomously and finish the task below. "
        "You do NOT share the parent conversation, so everything you need is here.\n\n"
        f"TASK: {task}")
    messages.append({"role": "user", "content": body, "_protect": True})
    return messages


def _record_partial(rec: ChildRecord, chunk: str) -> None:
    """Keep the child's streamed text on the record as it is produced (2026-11-30).

    A child killed by the wall-clock cap used to settle with output="" - the parent got
    "(the child produced no text)" and lost everything the child had already learned. The
    sink streams every content chunk here, so a capped child still reports its findings.
    """
    try:
        rec.partial += chunk
    except Exception:
        pass  # bookkeeping only - the child's run must never fail on a capture


async def run_child(rec: ChildRecord, context: Dict, continuation: str = "") -> str:
    """Run one child turn and return its final text. Raises only on wiring errors.

    `context` keys: provider, model, api_key, system_prompt, tool_orchestrator,
    ollama_tools, channel (parent ChatChannel), history (fork seed), working_root,
    enable_thinking, temperature.
    """
    provider = context.get("provider")
    if provider is None:
        raise RuntimeError("child context has no provider")
    channel = context.get("channel")
    if channel is None:
        raise RuntimeError("child context has no parent channel")

    sink = ChildSink(channel, rec.id, on_text=lambda chunk: _record_partial(rec, chunk))
    messages = _seed_messages(rec, context, rec.task, continuation)
    # The child's conversation is kept so send_message can continue it later.
    rec.messages = messages

    config = AgenticModeConfig(
        model=context.get("model") or "",
        temperature=float(context.get("temperature") or 0.6),
        # (2026-12 thinking) verbatim: a bool or the parent's canonical level.
        enable_thinking=context.get("enable_thinking", False),
        provider=provider,
        agent_name=context.get("agent_name") or "Jack",
        dna_content=context.get("system_prompt") or "",
        generic_tool_prompt=context.get("generic_tool_prompt") or "",
        websocket=sink,
        # Same conversation id: consent cards and background jobs belong to the parent
        # chat (the human approves a child's code; the parent collects its jobs).
        # (2026-11-30) owner_conv_id, not parent_conv_id: for a grandchild the parent is a
        # CHILD id, so consent cards and job notices keyed on it would land on a phantom
        # channel that nobody is listening to. The root chat is the real owner.
        conversation_id=rec.owner_conv_id or rec.parent_conv_id,
        stop_event=rec.stop_event,
        tool_orchestrator=context.get("tool_orchestrator"),
        ollama_tools=_filter_schema(context.get("ollama_tools") or []),
        api_key=context.get("api_key"),
        steer_source=rec.steers,
        # A child's steering is not user speech in the parent's history: it is not
        # persisted as a chat row (the child's own message list carries it).
        on_steer_applied=None,
        cycle=None,
        session_timer=None,
    )

    executor = ToolExecutor(
        context.get("tool_orchestrator"),
        None,
        provider=provider,
        current_model=config.model,
        api_key=config.api_key,
        cycle=None,
        forbidden_tools=CHILD_FORBIDDEN_TOOLS,
        child_id=rec.id,
        # (2026-12) CLIENT-LOCAL context for the child. A child may run background jobs
        # (job_* is not forbidden), so it needs the parent's channel for the notice lane;
        # subagent/ask_user_question are blocked by CHILD_FORBIDDEN_TOOLS both in the
        # filtered schema below and fail-closed in ToolExecutor.execute().
        channel=channel,
        ollama_tools=_filter_schema(context.get("ollama_tools") or []),
        generic_tool_prompt=context.get("generic_tool_prompt") or "",
        # (2026-12 thinking) verbatim: a bool or the parent's canonical level.
        enable_thinking=context.get("enable_thinking", False),
        temperature=float(context.get("temperature") or 0.6),
        role=context.get("role"),
    )

    # Per-child working root: the ContextVar copy belongs to THIS task, so concurrent
    # children and the parent never bleed into each other's folder.
    working_root = context.get("working_root")
    if working_root:
        try:
            from tools.utils import set_current_working_root
            set_current_working_root(working_root)
        except Exception as exc:  # pragma: no cover - tools module may not be loaded
            logger.debug(f"[SUBAGENT] working root publish failed for {rec.id}: {exc!r}")

    started = time.time()
    seed_len = len(messages)   # everything the child's own loop appends starts here
    # (2026-12) Recorded on the record so an interrupted/capped child's counters can be
    # derived from its OWN live message list (children.observed_work) even when this
    # finally block never runs - a cancel inside a long provider call skips it.
    rec.seed_len = seed_len
    result = ""   # (2026-12) bound BEFORE the try: if the loop raises, the finally block
                  # below still has to report what the child did (it reads `result`).
    try:
        result = await run_react_loop(config, messages, executor, [])
    finally:
        rec.turns += 1
        # (2026-12) react_loop's NATIVE COMPLETION path breaks WITHOUT appending the final
        # text reply to `messages` (it lives only in temp_thinking). So the child's own
        # conversation ended at its last tool result: a continuation never saw what the
        # child had already answered, and `steps` read 0 for a child that DID answer (a
        # child that replied "ok" reported steps: 0). Append the child's answer first, then
        # count - using the clean streamed text, and stripping the stats line + TASK DONE
        # marker from the fallback (kept noise would be imitated by the continuation).
        answer = sink.captured_text or _clean_child_answer(result)
        if answer.strip():
            last = messages[-1] if messages else {}
            if not isinstance(last, dict) or last.get("role") != "assistant" \
                    or (last.get("content") or "") != answer:
                messages.append({"role": "assistant", "content": answer})
        # Provider steps of the child's loop: the loop appends ONE assistant
        # message per provider call, so counting them past the seed gives an honest
        # "how much work did the child actually do" (the old 'turns: 1' lied).
        rec.steps += sum(1 for m in messages[seed_len:]
                         if isinstance(m, dict) and m.get("role") == "assistant")
        rec.tool_calls += sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "tool")
        # (2026-12) Where this turn's counting ended, so observed_work() can add only the
        # messages the child's loop appended AFTER the finally ran (never a double count).
        rec.counted_upto = len(messages)
        # (2026-11-30) Keep the child's LAST tool results on the record. `messages` is owned
        # here, so it survives a wall-clock cancellation: a child cut off mid-work still
        # reports what it actually read instead of only its opening sentence.
        rec.tool_tail = [str(m.get("content") or "")[:400]
                         for m in messages if isinstance(m, dict) and m.get("role") == "tool"][-6:]
        logger.info(f"[SUBAGENT] child {rec.id} turn finished in {time.time() - started:.1f}s "
                    f"(turns={rec.turns}, tools={rec.tool_calls}, chars={len(sink.captured_text)})")
    # Prefer the captured stream over the assembled answer: the loop's final string
    # carries the spliced stats line + TASK DONE marker, which is parent UI noise - so
    # even the fallback is cleaned before it rides into the parent's notice.
    return sink.captured_text or _clean_child_answer(result)


def child_context(*, provider: Any, model: str, api_key: Optional[str], agent_name: str,
                  dna_content: str, generic_tool_prompt: str, tool_orchestrator: Any,
                  ollama_tools: List[Dict], channel: Any, history: Optional[List[Dict]],
                  working_root: Optional[str], enable_thinking: bool,
                  temperature: float, role: Optional[str] = None) -> Dict:
    """Assemble the context dict a child needs, from the parent's live turn config."""
    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "agent_name": agent_name,
        "system_prompt": f"{dna_content}\n\n{generic_tool_prompt}\n\n"
                         "## SUBAGENT CONTRACT\n"
                         "You are a delegated child agent. Nobody is watching you live: "
                         "your final text IS the report the parent receives, so make it "
                         "self-contained (what you did, what you found, what failed). "
                         "Do not ask the user questions - you have no question surface. "
                         "You have no screen/browser/image tools; use files, search, web "
                         "and python. Finish with a plain text answer.",
        "generic_tool_prompt": generic_tool_prompt,
        "tool_orchestrator": tool_orchestrator,
        "ollama_tools": ollama_tools,
        "channel": channel,
        "history": history or [],
        "working_root": working_root,
        "enable_thinking": enable_thinking,
        "temperature": temperature,
        "role": role,
    }
