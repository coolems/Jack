"""local_tools — CLIENT-side agent tools (subagents, background jobs, questions).

WHY THESE ARE CLIENT-LOCAL: their whole job is CLIENT runtime state — a child's runner, a
job's output ring, a consent card on THIS machine's UI. A SERVER-delivered tool runs inside
the loader sandbox on a worker thread (RemoteToolOrchestrator.execute_tool ->
asyncio.to_thread), where it cannot own an asyncio.Task, hold a channel, or await a future.
So these live here, are awaited IN PLACE, and are never delivered by the SERVER.

Every definition lives in TOOL_DEFINITIONS (one dict, same OpenAI shape as delivered
tools); every handler is a module-level async function. local_registry.py merges the
definitions into the schema the model sees and dispatches the calls.

Design rules baked into the descriptions:
  * subagent returns the child id IMMEDIATELY — the report arrives later as a notice;
  * job_output is the only way to read a job's stream, and the registry keeps the cursor;
  * ask_user_question never invents an answer: a timed question returns PENDING, which is
    not a refusal; it states what the user asked for and every option must deliver THAT -
    options that ask for more input or deliver something else are dropped; and the card
    offers only those candidates plus a free-text answer (no "let the AI decide" button).
"""

import asyncio
import json
import logging
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from config import (
    JOB_MAX_WAIT_TIMEOUT_SEC,
    JOB_WAIT_TIMEOUT_SEC,
    QUESTION_MAX_OPTIONS,
    QUESTION_TIMEOUT_DEFAULT_SEC,
    SUBAGENT_MAX_CONCURRENT_PER_CONV,
    SUBAGENT_MAX_DEPTH,
)

logger = logging.getLogger("COOLEMS.LocalTools")


def _err(msg: str) -> str:
    """Tool results are strings; a failure must read as a clear, actionable error."""
    return f"ERROR: {msg}"


# --- call context ---------------------------------------------------------
# Set by RemoteToolOrchestrator.execute_tool around a LOCAL call, on the event-loop
# thread. threading.local because a sync tool runs in a worker thread via to_thread; a
# LOCAL tool is awaited in place, so the loop thread owns it — the same anchor keeps both
# paths correct and concurrent chats never cross wires.
_CTX_KEY = "_coolems_local_tool_context"


def _ctx() -> Dict[str, Any]:
    mod = __import__("sys").modules.get(_CTX_KEY)
    local = getattr(mod, "context_local", None) if mod is not None else None
    ctx = getattr(local, "context", None) if local is not None else None
    return ctx if isinstance(ctx, dict) else {}


def set_local_context(loop, channel, websocket, conversation_id, ollama_tools,
                      generic_tool_prompt, enable_thinking, temperature,
                      parent_history, session_timer, role=None) -> None:
    """Install the calling turn's context for the duration of one local tool call."""
    import sys as _sys
    import types as _types

    mod = _sys.modules.get(_CTX_KEY)
    if mod is None:
        mod = _types.ModuleType(_CTX_KEY)
        mod.context_local = threading.local()
        _sys.modules[_CTX_KEY] = mod
    mod.context_local.context = {
        "loop": loop,
        "channel": channel,
        "websocket": websocket,
        "conversation_id": conversation_id,
        "ollama_tools": ollama_tools or [],
        "generic_tool_prompt": generic_tool_prompt or "",
        "enable_thinking": bool(enable_thinking),
        "temperature": float(temperature or 0.6),
        "parent_history": parent_history or [],
        "session_timer": session_timer,
        "role": role,
    }


def clear_local_context() -> None:
    import sys as _sys
    mod = _sys.modules.get(_CTX_KEY)
    if mod is not None:
        mod.context_local.context = None


# ============================================================
# SUBAGENTS
# ============================================================

def _child_context() -> Dict[str, Any]:
    """What a child needs, taken from the CALLING turn — never guessed.

    The parent's schema is passed as-is; child_run filters it to the child's allowed set.
    The fork seed is the parent's COMPLETED conversation only (build_messages' history),
    so the child cannot see the parent's in-flight tool results.
    """
    ctx = _ctx()
    return {
        "ollama_tools": ctx.get("ollama_tools") or [],
        "generic_tool_prompt": ctx.get("generic_tool_prompt") or "",
        "enable_thinking": bool(ctx.get("enable_thinking")),
        "temperature": float(ctx.get("temperature") or 0.6),
        "history": ctx.get("parent_history") or [],
        "role": ctx.get("role"),
    }


async def subagent(task: str, label: str = "", mode: str = "spawn") -> str:
    """Delegate a task to a child agent; returns the child id immediately."""
    from app.runtime.children import get_child_registry

    task = (task or "").strip()
    if not task:
        return _err("task must not be empty - state the whole task, including paths.")
    registry = get_child_registry()
    if registry is None:
        return _err("subagent runtime is not wired in this CLIENT process - do the work yourself.")
    ctx = _ctx()
    conv_id = ctx.get("conversation_id")
    if not conv_id:
        return _err("no conversation context - delegation needs an owning chat.")
    mode = (mode or "spawn").strip().lower()
    if mode not in ("spawn", "fork"):
        return _err("mode must be 'spawn' or 'fork'.")

    try:
        child_id = registry.start(parent_conv_id=conv_id, mode=mode, task=task,
                                  label=label, context=_child_context())
    except RuntimeError as e:
        # Depth / concurrency budget: the model must see the limit, not a stack trace.
        return _err(f"{e} (limits: depth {SUBAGENT_MAX_DEPTH}, "
                   f"{SUBAGENT_MAX_CONCURRENT_PER_CONV} concurrent children per chat)")
    except Exception as e:
        logger.error(f"[SUBAGENT] start failed: {e!r}")
        return _err(f"could not start the child: {type(e).__name__}: {e}")

    cap = registry.capacity(conv_id)
    slots = (f"{cap['live']}/{cap['max']} slots now live"
             + (f", {cap['free']} free" if cap['free'] else " - budget FULL"))

    from app.runtime.runtime_store import save_runtime_row
    rec = registry.get(child_id)
    # (2026-11-30) The ledger row and the UI row belong to the OWNER chat (the root of the
    # lineage), not to conv_id: for a grandchild conv_id is a CHILD id, which would create
    # a ledger row for a conversation that does not exist.
    owner = rec.get("owner_conv_id") or conv_id
    save_runtime_row(conv_id=owner, kind="child", status="running",
                     label=label or task[:80], mode=mode,
                     depth=int(rec.get("depth", 1)), row_id=child_id)
    try:
        channel = ctx.get("channel")
        if channel is not None:
            # record_runtime() routes to the live turn's recorder (a row of the turn's
            # block + persisted turn_log) or to a plain frame when no turn is running.
            await channel.record_runtime("child", child_id, label=label or task[:80],
                                         mode=mode, depth=int(rec.get("depth", 1)),
                                         status="running")
    except Exception:
        pass
    return (
        f"DELEGATED: child {child_id} ({mode}, depth {rec.get('depth', 1)}) is running now.\n"
        f"Fan-out budget: {slots}.\n"
        "This result does NOT contain the answer - the report arrives later as a runtime "
        "notice ('SUBAGENT SETTLED'), at your next step or as a follow-up turn.\n"
        "Keep working on INDEPENDENT steps now; do not wait for this child. Before your "
        "final answer, confirm every child you delegated has settled (list_agents) and you "
        "have read its report. Never present a child's report as your own work."
    )


async def subagent_fork(task: str, label: str = "") -> str:
    """Fork variant: same as subagent with mode='fork' (inherits completed turns)."""
    return await subagent(task=task, label=label, mode="fork")


async def send_message(agent_id: str, message: str) -> str:
    """Steer a running child, or continue a settled one's conversation."""
    from app.runtime.children import get_child_registry

    registry = get_child_registry()
    if registry is None:
        return _err("subagent runtime is not wired in this CLIENT process.")
    ctx = _ctx()
    conv_id = ctx.get("conversation_id")
    try:
        result = registry.send_message(agent_id, message, conv_id)
    except KeyError as e:
        return _err(f"{e}. Call list_agents to see the ids you actually have.")
    except PermissionError:
        return _err("That child belongs to another conversation - you may only address your own.")
    if not result.get("ok"):
        return _err(result.get("reason") or "the message was not accepted")
    if result.get("mode") == "steering":
        return (f"Steering queued for child {agent_id} (steer {result['steer_id'][:8]}). "
                "It is injected at the child's next step; the child keeps running.")
    try:
        reply = await registry.start_continuation(agent_id, message, conv_id, _child_context())
    except Exception as e:
        return _err(f"continuation failed: {type(e).__name__}: {e}")
    return f"Child {agent_id} continued:\n{reply[:4000]}"


async def interrupt_agent(agent_id: str, reason: str = "") -> str:
    """Stop one child (and its descendants)."""
    from app.runtime.children import get_child_registry

    registry = get_child_registry()
    if registry is None:
        return _err("subagent runtime is not wired in this CLIENT process.")
    ctx = _ctx()
    try:
        result = registry.interrupt(agent_id, ctx.get("conversation_id"),
                                    reason or "interrupted by the agent")
    except KeyError as e:
        return _err(f"{e}. Call list_agents to see the ids you actually have.")
    except PermissionError:
        return _err("That child belongs to another conversation.")
    if not result.get("ok"):
        return f"Child {agent_id} already finished (status={result.get('status')}) - nothing to interrupt."
    return f"Child {agent_id} is stopping. Its aborted report arrives as a notice."


async def list_agents(scope: str = "children", agent_id: str = "") -> str:
    """List this conversation's children, or read one child's report."""
    from app.runtime.children import get_child_registry

    registry = get_child_registry()
    if registry is None:
        return _err("subagent runtime is not wired in this CLIENT process.")
    ctx = _ctx()
    conv_id = ctx.get("conversation_id")
    if agent_id:
        try:
            report = registry.read_output(agent_id, conv_id)
        except KeyError as e:
            return _err(f"{e}. Call list_agents to see the ids you actually have.")
        except PermissionError:
            return _err("That child belongs to another conversation.")
        return json.dumps(report, ensure_ascii=False, indent=2)
    rows = registry.list_children(conv_id or "",
                                  "descendants" if scope == "descendants" else "children")
    # (2026-11-30) The fan-out budget rides on the listing, so a delegation decision is
    # made from real numbers: the model can see how many slots are live BEFORE it tries
    # to delegate again, instead of discovering the cap through an error.
    cap = registry.capacity(conv_id or "")
    header = (f"Fan-out budget: {cap['live']}/{cap['max']} live"
              + (f", {cap['free']} free" if cap["free"] else " - FULL (wait or interrupt one)"))
    if not rows:
        return f"{header}\nNo subagents have been delegated in this conversation."
    return header + "\n" + json.dumps(rows, ensure_ascii=False, indent=2)


# ============================================================
# BACKGROUND JOBS
# ============================================================

def _jobs_or_error():
    from app.runtime.jobs import get_job_registry
    registry = get_job_registry()
    if registry is None:
        return None, _err("the background-job runtime is not wired in this CLIENT process.")
    return registry, ""


async def job_list() -> str:
    """List this chat's background jobs (live + settled)."""
    registry, err = _jobs_or_error()
    if registry is None:
        return err
    ctx = _ctx()
    rows = registry.list(ctx.get("conversation_id"))
    if not rows:
        return "No background jobs for this conversation."
    return json.dumps(rows, ensure_ascii=False, indent=2)


async def job_output(job_id: str, wait: bool = False, timeout_ms: int = 0) -> str:
    """Read a job's output since your last read (the registry keeps your cursor)."""
    registry, err = _jobs_or_error()
    if registry is None:
        return err
    ctx = _ctx()
    conv_id = ctx.get("conversation_id")
    try:
        timeout = float(timeout_ms) if timeout_ms else JOB_WAIT_TIMEOUT_SEC * 1000.0
        timeout = min(max(0.0, timeout), JOB_MAX_WAIT_TIMEOUT_SEC * 1000.0)
        # ONE consuming read per job, serialized by the registry: two job_output calls in
        # the same batch would otherwise share the cursor and eat each other's output.
        steer_check = None
        if wait:
            channel = ctx.get("channel")
            maybe = getattr(channel, "has_unapplied_steer", None) if channel else None
            if callable(maybe):
                steer_check = maybe
        view, steered, read = await registry.consume(
            job_id, conv_id, wait_ms=timeout if wait else 0.0, steer_check=steer_check)
        if steered:
            return (f"[status: {view['status']}, user steered] - the user redirected the task "
                    f"while this job was running. Job {job_id} is still running; read it again "
                    "when relevant, or job_kill it.")
    except KeyError as e:
        return _err(f"{e}. Call job_list to see the ids you actually have.")
    except PermissionError:
        return _err("That job belongs to another conversation.")
    except RuntimeError as e:
        return _err(str(e))

    job = read["job"]
    parts = [f"job {job_id}: status={job['status']}"
             + (f" detail={job['detail']}" if job.get("detail") else "")
             + (f" progress={job['progress']}" if job.get("progress") else "")]
    if read["lossy"]:
        parts.append("[earlier bytes were evicted from the ring - the spill file holds the complete stream]")
    if job.get("spill_path"):
        parts.append(f"[complete stream file: {job['spill_path']}]")
    parts.append(read["text"] or "(no new output since your last read)")
    if read["result"]:
        parts.append("--- result ---\n" + read["result"])
    if job["status"] in ("running", "stopping"):
        parts.append(f"[still running - unread bytes: {job['unread_bytes']}]")
    return "\n".join(parts)


async def job_kill(job_id: str, reason: str = "") -> str:
    """Cancel a background job."""
    registry, err = _jobs_or_error()
    if registry is None:
        return err
    ctx = _ctx()
    try:
        state = registry.kill(job_id, ctx.get("conversation_id"),
                             reason or "cancelled by the agent")
    except KeyError as e:
        return _err(f"{e}. Call job_list to see the ids you actually have.")
    except PermissionError:
        return _err("That job belongs to another conversation.")
    if state == "already-finished":
        return f"Job {job_id} already finished - nothing to kill."
    # (2026-12) The kill CLAIMS the settlement: this tool result IS the delivery, so no
    # notice arrives afterwards. The old wording promised "its terminal notice arrives when
    # it settles", which the model waited for and never got. Say what actually happens.
    return (f"Job {job_id} is stopping (cause: kill). No notice will arrive - this result is "
            "the delivery. Read its final output with job_output when relevant.")


# ============================================================
# ask_user_question (rides the shared consent dialog, kind='question')
# ============================================================

#: An option is a CANDIDATE: something the AI can deliver the moment the user clicks it.
#: An option that hands the work back to the user ("paste the code in the chat", "I'll
#: describe it") is NOT a candidate - it asks a question inside an answer button, so the
#: card answers nothing and the user still has to write the whole request. That is exactly
#: what the user reported on 2026-11-13: "run a python code for me" produced
#: "Paste the code / Run a file / A task I'll describe / Run the test suite" instead of
#: concrete offers like "a simple game", "a small app", "a quiz".
_META_OPTION_RE = re.compile(
    r"\b(paste|type|describe|upload|provide|specify|tell me|your ?answer|free ?text|"
    r"i'?ll ?describe|no ?preference|whatever)\b",
    re.IGNORECASE,
)

#: Bounds for one candidate as it reaches the card (a button is read in one glance).
OPTION_LABEL_MAX_CHARS = 90
OPTION_DETAIL_MAX_CHARS = 160
OPTION_DELIVERABLE_MAX_CHARS = 60

#: Words that carry no topic of their own, so a requirement match ignores them.
_REQUIREMENT_STOPWORDS = frozenset({
    "the", "and", "for", "with", "you", "your", "me", "my", "please", "plz", "can",
    "will", "would", "what", "which", "some", "any", "this", "that", "here", "now",
    "make", "give", "have", "from", "into", "about", "just", "a", "an", "of", "to",
    "in", "on", "it", "is", "do", "i", "as", "or", "be", "we", "us", "so", "if",
})

#: Technologies an option can name. When the user asked for one of them, an option that
#: names a DIFFERENT one is off-topic and never reaches the card (user request 2026-11-13:
#: "run Python code" must not offer "a JavaScript website" or "Snake in one HTML file").
#: Ambiguous short words ('c', 'go') are left out on purpose - 'C:\\Transfer' or 'let's go'
#: must not be read as a language.
_TECH_WORDS = frozenset({
    "python", "javascript", "typescript", "js", "html", "css", "java", "kotlin", "swift",
    "rust", "golang", "cpp", "c++", "sql", "php", "ruby", "perl", "powershell", "bash",
    "shell", "elixir", "dart", "scala", "matlab", "excel",
})


def _topic_tokens(text: str) -> set:
    """Content words of *text* (lowercase, stopwords dropped)."""
    return {w for w in re.findall(r"[a-z0-9+]+", str(text).lower()) if w not in _REQUIREMENT_STOPWORDS}


def _tech_tokens(text: str) -> set:
    """The technologies named in *text* (lowercase whole words only)."""
    return {w for w in re.findall(r"[a-z0-9+]+", str(text).lower()) if w in _TECH_WORDS}


def _is_off_topic(option: Dict[str, str], req_tech: set, req_tokens: set) -> bool:
    """True when the option does not serve what the user asked for.

    Two checks, both against the option's own words (deliverable, label, detail):

      * it names a technology the user did not ask for ('HTML page' when they asked for
        Python) - the mismatch the user complained about;
      * it shares no content word with the requirement at all ('Order lunch'), so the card
        never offers unrelated work.

    An option that names the requested technology always passes, which is why every option
    must state its deliverable ('Python game'). A variant of the same ask that shares a
    word ('run its pytest suite') is kept.
    """
    text = option["deliverable"] + " " + option["label"] + " " + option["detail"]
    if req_tech and _tech_tokens(text) - req_tech:
        return True
    if req_tokens and not (_topic_tokens(text) & req_tokens):
        return True
    return False


def _normalize_options(options: Optional[list], requirement: str = "") -> Tuple[List[Dict[str, str]], List[str], List[str]]:
    """Turn the model's options into numbered candidates for the card.

    Accepts a plain string or ``{"label", "detail", "deliverable", "recommended"}`` per
    option, so an older model (or an older recording) still renders. Returns
    ``(kept, dropped, off_topic)``:

      * kept       - candidates that serve *requirement*, deduped case-insensitively,
                     capped at QUESTION_MAX_OPTIONS, each ``{"label", "detail",
                     "deliverable", "recommended"}``. The recommended one - the option
                     closest to what the user asked - is moved to position 1, and exactly
                     one option is flagged, so the card always has one visible favourite.
      * dropped    - options the user cannot act on (they ask for input instead of work).
      * off_topic  - options that do not serve the requirement.
    """
    kept: List[Dict[str, str]] = []
    dropped: List[str] = []
    off_topic: List[str] = []
    seen = set()
    req_tech = _tech_tokens(requirement)
    req_tokens = _topic_tokens(requirement)
    for raw in (options or []):
        if isinstance(raw, dict):
            label = str(raw.get("label") or "").strip()
            detail = str(raw.get("detail") or "").strip()
            deliverable = str(raw.get("deliverable") or "").strip()
            preferred = bool(raw.get("recommended"))
        else:
            label, detail, deliverable, preferred = str(raw or "").strip(), "", "", False
        if not label:
            continue
        if _META_OPTION_RE.search(label):
            dropped.append(label)
            continue
        option = {"label": label[:OPTION_LABEL_MAX_CHARS],
                  "detail": detail[:OPTION_DETAIL_MAX_CHARS],
                  "deliverable": deliverable[:OPTION_DELIVERABLE_MAX_CHARS],
                  "recommended": preferred}
        if _is_off_topic(option, req_tech, req_tokens):
            off_topic.append(label)
            continue
        key = option["label"].lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(option)
    kept = kept[:QUESTION_MAX_OPTIONS]
    # The favourite sits first: the card reads top-down, and option 1 is the one the UI
    # marks as the closest match to the user's request.
    preferred_index = next((i for i, o in enumerate(kept) if o["recommended"]), None)
    if preferred_index is not None and preferred_index != 0:
        kept.insert(0, kept.pop(preferred_index))
    for i, option in enumerate(kept):
        option["recommended"] = (i == 0)
    return kept, dropped, off_topic


async def ask_user_question(question: str, options: Optional[list] = None,
                            context: str = "", mode: str = "blocking",
                            timeout_sec: int = 0, requirement: str = "") -> str:
    """Ask the user a question on a card. Returns the answer, or PENDING in timed mode."""
    ctx = _ctx()
    conv_id = ctx.get("conversation_id")
    websocket = ctx.get("websocket")
    channel = ctx.get("channel")
    if not conv_id or websocket is None:
        return _err("no live chat channel - the question cannot be shown to the user.")

    from app.chat_bus import get_chat_bus
    from app.chat_bus.consent_dialog import get_approval_state

    state = get_approval_state(get_chat_bus(), conv_id)
    if state is None:
        return _err("no approval state for this chat - the question cannot be asked now.")

    opts, dropped, off_topic = _normalize_options(options, requirement)
    if options and not opts:
        # The card would offer nothing clickable, so do not show a hollow card: tell the
        # model what a candidate is and let it re-issue the question with real offers.
        parts = []
        if dropped:
            parts.append("asked the user for input: " + ", ".join(dropped))
        if off_topic:
            parts.append("names something the user did not ask for: " + ", ".join(off_topic))
        why = " - " + "; ".join(parts) if parts else ""
        return _err(
            "none of your options is something the user can pick" + why + ". An option must be "
            f"work YOU can do right now AND a way of satisfying what the user asked"
            + (f" ({requirement})" if requirement else "") + ". Re-issue the question with 2-"
            f"{QUESTION_MAX_OPTIONS} candidates that all deliver that, closest first with "
            "recommended=true, and name each one's deliverable in the technology the user asked "
            "for - for 'run some Python': 'A simple game - Snake, one Python file' "
            "(deliverable 'Python game'), 'A todo app - terminal, saves to a file' "
            "(deliverable 'Python terminal app'), 'A quiz - 10 questions, scored' "
            "(deliverable 'Python terminal quiz'). The card already has a free-text field for "
            "'tell me exactly what you want', so never add an option for that."
        )

    payload = json.dumps({"question": question, "options": opts, "context": context,
                          "requirement": requirement or ""},
                         ensure_ascii=False)
    request_id = state.new_request_id()
    if channel is not None:
        channel.add_pending_question(request_id, {"question": question, "options": opts,
                                                  "context": context, "mode": mode,
                                                  "requirement": requirement or ""})
    try:
        await websocket.send_text(json.dumps({
            "type": "exec_approval_request",
            "conv_id": conv_id,
            "request_id": request_id,
            "kind": "question",
            "code": payload,
            "description": context or "",
        }))
        if channel is not None:
            # The durable row lists the candidate NAMES only - the details belong to the
            # transient card, not to the turn ledger.
            await channel.record_runtime("question", request_id, question=question,
                                         options=[o["label"] for o in opts], pending=True)
    except Exception as e:
        if channel is not None:
            channel.drop_pending_question(request_id)
        return _err(f"the question card could not be delivered ({e}) - decide yourself and state the assumption you are making.")

    timer = ctx.get("session_timer")
    if timer is not None:
        timer.pause()   # the user deciding is not working time
    try:
        if mode == "timed":
            wait = float(timeout_sec or QUESTION_TIMEOUT_DEFAULT_SEC)
            try:
                answer = await asyncio.wait_for(
                    state.wait_for_decision(request_id, kind="question"), timeout=wait)
            except asyncio.TimeoutError:
                # The card stays live: the answer arrives later as a notice / follow-up turn.
                return (f"PENDING: the user has not answered within {int(wait)}s. This is NOT a "
                        "refusal - the card stays open. Continue with independent work that does "
                        "not depend on the answer; the answer will arrive as a runtime notice.")
        else:
            answer = await state.wait_for_decision(request_id, kind="question")
    finally:
        if timer is not None:
            timer.resume()

    if channel is not None:
        channel.drop_pending_question(request_id)
        try:
            await channel.emit_frame({"type": "cycle_question", "conv_id": conv_id,
                                      "turn_id": getattr(channel, "turn_id", "") or "",
                                      "request_id": request_id, "decision": answer,
                                      "pending": True})
        except Exception:
            pass

    if answer in ("deny", "unavailable"):
        # The card has no Skip button, so this only happens when the card could not be
        # answered (channel closed, stale request) - fail closed, never invent an answer.
        return ("USER DECISION: the question could not be answered. Do not re-ask it; state "
                "the assumption you are making and continue, or report what you could not decide.")
    if answer == "allow_category_until_done":
        return ("USER DECISION: you may decide questions of this kind yourself for the rest of "
                "this task. State each choice you make in your final answer.")
    return f"USER ANSWER: {answer}"


# ============================================================
# Definitions (one dict, OpenAI shape - the schema the model sees)
# ============================================================

TOOL_DEFINITIONS: Dict[str, Dict] = {
    "subagent": {
        "type": "function",
        "function": {
            "name": "subagent",
            "description": (
                "Delegate a self-contained task to a subagent (a separate agent working in its own "
                "context) so it does not consume this conversation's context: research, a scoped "
                "implementation, an analysis. Returns the child id IMMEDIATELY - the report arrives "
                "later as a runtime notice, not as this result. mode 'spawn' = fresh context (the "
                "task must stand alone); mode 'fork' = seeded with this conversation's completed "
                "turns (it does not see the current in-flight turn). You receive the child's result, "
                "not its intermediate steps. Children have no screen, browser, image or delegation "
                "tools. Start independent subtasks at once, keep working, and collect every child "
                "before your final answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string",
                             "description": "The complete task. A spawn child shares no context, so include everything it needs."},
                    "label": {"type": "string", "description": "One-line description for the UI and the settlement notice."},
                    "mode": {"type": "string", "enum": ["spawn", "fork"],
                             "description": "spawn = fresh context (default); fork = inherits completed turns."},
                },
                "required": ["task"],
            },
        },
    },
    "subagent_fork": {
        "type": "function",
        "function": {
            "name": "subagent_fork",
            "description": (
                "Delegate a task to a subagent that INHERITS this conversation: a child seeded with "
                "all completed turns so far (it does not see the current in-flight turn). Use it for "
                "a follow-up analysis, a review or a continuation that builds on what already "
                "happened, without spending this conversation's context on the work itself. Returns "
                "the child id immediately; the report arrives later as a runtime notice."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string",
                             "description": "The task. The child already sees this conversation's completed turns, so state only what is new."},
                    "label": {"type": "string", "description": "One-line description for the UI and the settlement notice."},
                },
                "required": ["task"],
            },
        },
    },
    "send_message": {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "Send a message to a running subagent (steering: injected at the child's next safe "
                "step, the child keeps working) or continue a finished child's conversation. Only "
                "children of THIS conversation can be addressed. Use it to correct a child's "
                "direction mid-run or to ask a settled child for more detail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "The child id returned by subagent/subagent_fork."},
                    "message": {"type": "string", "description": "The instruction or follow-up question."},
                },
                "required": ["agent_id", "message"],
            },
        },
    },
    "interrupt_agent": {
        "type": "function",
        "function": {
            "name": "interrupt_agent",
            "description": (
                "Stop one running subagent (and its descendants). Its report then settles with "
                "stop_reason 'aborted' and arrives as a notice. Use it when a child is going the "
                "wrong way or the task no longer needs that work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "The child id to stop."},
                    "reason": {"type": "string", "description": "Why it is being stopped (recorded in the ledger)."},
                },
                "required": ["agent_id"],
            },
        },
    },
    "list_agents": {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": (
                "List the subagents of this conversation: id, mode, depth, status, stop reason, "
                "turn/tool counts, output size. scope 'children' = direct children (default); "
                "'descendants' = the whole subtree. Pass agent_id to read one child's report. Read "
                "this before your final answer to confirm nothing you delegated is still running."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["children", "descendants"]},
                    "agent_id": {"type": "string",
                                 "description": "Optional: read one child's report instead of listing."},
                },
                "required": [],
            },
        },
    },
    "job_list": {
        "type": "function",
        "function": {
            "name": "job_list",
            "description": (
                "List this conversation's background jobs (long-running python_exec, builds, tests): "
                "id, kind, label, status, progress, unread bytes, spill file. Read this before your "
                "final answer so no job you started is left unread."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    "job_output": {
        "type": "function",
        "function": {
            "name": "job_output",
            "description": (
                "Read a background job's output since YOUR LAST read (the registry keeps your cursor, "
                "so you never get the same bytes twice). wait=true blocks until the job settles or the "
                f"timeout expires; a wait that yields to a user steer says so instead of pretending "
                "the job finished. Returns status, unread output and the job's result (delivered once). "
                "Do not busy-poll: read a job once, keep working on independent steps, and collect "
                "every still-relevant job before the final answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "wait": {"type": "boolean", "description": "Wait for settlement (default false)."},
                    "timeout_ms": {"type": "integer",
                                   "description": f"Wait bound in ms (default {JOB_WAIT_TIMEOUT_SEC * 1000}, max {JOB_MAX_WAIT_TIMEOUT_SEC * 1000})."},
                },
                "required": ["job_id"],
            },
        },
    },
    "job_kill": {
        "type": "function",
        "function": {
            "name": "job_kill",
            "description": (
                "Cancel a background job. This tool result IS the delivery of the kill - no "
                "notice arrives afterwards, so read the job's final output with job_output "
                "when you need it. The reason is recorded in the durable ledger."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "reason": {"type": "string", "description": "Why it is cancelled (recorded in the ledger)."},
                },
                "required": ["job_id"],
            },
        },
    },
    "ask_user_question": {
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "description": (
                "Ask the USER a question on a card in the chat: what they asked for, one "
                f"question, up to {QUESTION_MAX_OPTIONS} numbered candidates, plus a free-text "
                f"answer. mode 'blocking' waits for the user with no time limit (the session "
                f"clock pauses). mode 'timed' waits at most {QUESTION_TIMEOUT_DEFAULT_SEC}s and "
                "then returns PENDING - that is NOT a refusal: the card stays answerable, so "
                "keep doing independent work and the answer arrives later as a runtime notice. "
                "Use this when a decision genuinely belongs to the user; never invent an answer.\n\n"
                "FIRST state 'requirement': what the user actually asked for, in one line. Then "
                f"offer 2-{QUESTION_MAX_OPTIONS} candidates that ALL deliver that requirement - "
                "if the user asked for Python code, every option is Python code, and an option "
                "about something else is rejected. Each option is work YOU can do the moment it "
                "is clicked ('A snake game - one Python file'), never a request for more input "
                "('paste the code', 'describe it'), and each names its deliverable in the "
                "technology the user asked for ('Python game'). Put the candidate closest to the "
                "request first and set recommended=true on it: the card marks option 1 as the "
                "favourite. Do not add an option like 'tell me what you want' - the card always "
                "has a free-text field for that, and there is no skip button: the user picks or "
                "types."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "requirement": {"type": "string",
                                    "description": "What the user asked for, one line (e.g. 'run Python code for me'). Every option must satisfy this."},
                    "question": {"type": "string", "description": "The question, one sentence."},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string",
                                          "description": "What you will do, short: 'A snake game - one Python file'."},
                                "detail": {"type": "string",
                                          "description": "One line the user reads to choose: what they get."},
                                "deliverable": {"type": "string",
                                                "description": "The artifact you produce, named in the technology the user asked for: 'Python game', 'Python terminal quiz'."},
                                "recommended": {"type": "boolean",
                                                "description": "true on the candidate closest to the requirement; it is moved to position 1 and shown as the favourite."},
                            },
                            "required": ["label"],
                            "additionalProperties": False,
                        },
                        "description": (
                            f"2-{QUESTION_MAX_OPTIONS} candidates that all satisfy 'requirement', "
                            "closest first (plain strings are accepted too). Never an option that "
                            "asks the user for input or delivers something else."
                        ),
                    },
                    "context": {"type": "string", "description": "Why you are asking, one or two lines."},
                    "mode": {"type": "string", "enum": ["blocking", "timed"]},
                    "timeout_sec": {"type": "integer", "description": "timed mode wait bound."},
                },
                "required": ["requirement", "question"],
            },
        },
    },
}

#: name -> handler. Every name in TOOL_DEFINITIONS must have one.
TOOL_HANDLERS: Dict[str, Any] = {
    "subagent": subagent,
    "subagent_fork": subagent_fork,
    "send_message": send_message,
    "interrupt_agent": interrupt_agent,
    "list_agents": list_agents,
    "job_list": job_list,
    "job_output": job_output,
    "job_kill": job_kill,
    "ask_user_question": ask_user_question,
}
