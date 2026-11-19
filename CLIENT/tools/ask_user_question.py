"""ask_user_question - the CLIENT-local "ask the human" tool.

The card shows what the user asked for, one question, numbered candidates and a
free-text answer. It rides the consent dialog (kind 'question') but is not a
permission gate. CLIENT-LOCAL because the card and its answer Future live on this
machine, which a sandboxed worker-thread tool cannot hold (see local_ask_registry.py)."""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from client_config import QUESTION_MAX_OPTIONS, QUESTION_TIMEOUT_DEFAULT_SEC

logger = logging.getLogger("COOLEMS.LocalTools.Question")

#: The one tool name this module owns (the schema promise and the dispatcher must agree).
TOOL_NAME = "ask_user_question"

#: An option is a CANDIDATE: something the AI can deliver the moment the user clicks it.
#: An option that hands the work back to the user ("paste the code in the chat", "I'll
#: describe it") is NOT a candidate - it asks a question inside an answer button, so the
#: card answers nothing and the user still has to write the whole request.
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
#: names a DIFFERENT one is off-topic and never reaches the card ("run Python code" must
#: not offer "a JavaScript website").
#: Ambiguous short words ('c', 'go') are left out on purpose - 'C:\\Transfer' or 'let's go'
#: must not be read as a language.
_TECH_WORDS = frozenset({
    "python", "javascript", "typescript", "js", "html", "css", "java", "kotlin", "swift",
    "rust", "golang", "cpp", "c++", "sql", "php", "ruby", "perl", "powershell", "bash",
    "shell", "elixir", "dart", "scala", "matlab", "excel",
})


def _err(msg: str) -> str:
    """Tool results are strings; a failure must read as a clear, actionable error."""
    return f"ERROR: {msg}"


def _topic_tokens(text: str) -> set:
    """Content words of *text* (lowercase, stopwords dropped)."""
    return {w for w in re.findall(r"[a-z0-9+]+", str(text).lower()) if w not in _REQUIREMENT_STOPWORDS}


def _tech_tokens(text: str) -> set:
    """The technologies named in *text* (lowercase whole words only)."""
    return {w for w in re.findall(r"[a-z0-9+]+", str(text).lower()) if w in _TECH_WORDS}


def _is_off_topic(option: Dict[str, str], req_tech: set, req_tokens: set) -> bool:
    """True when the option does not serve what the user asked for.

    Checks the option's own words: it names a technology the user did not ask for, or it
    shares no content word with the requirement."""
    text = option["deliverable"] + " " + option["label"] + " " + option["detail"]
    if req_tech and _tech_tokens(text) - req_tech:
        return True
    if req_tokens and not (_topic_tokens(text) & req_tokens):
        return True
    return False


def normalize_options(options: Optional[list], requirement: str = "") -> Tuple[List[Dict[str, str]], List[str], List[str]]:
    """Turn the model's options into numbered candidates for the card.

    Accepts a plain string or {"label", "detail", "deliverable", "recommended"} per option.
    Returns (kept, dropped, off_topic): candidates serving *requirement* (deduped, capped,
    recommended first and exactly one flagged), options asking for input instead of work,
    and options that do not serve the requirement."""
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


def question_definition() -> Dict:
    """The OpenAI-shaped definition the model sees, same shape as delivered tools."""
    return {
        "type": "function",
        "function": {
            "name": "ask_user_question",
            "description": (
                "Ask the USER a question on a card in the chat: what they asked for, one "
                f"question, up to {QUESTION_MAX_OPTIONS} numbered candidates, plus a free-text "
                f"answer. mode 'blocking' waits for the user with no time limit (the session "
                f"clock pauses) - use it when the answer is needed to continue. mode 'timed' "
                f"waits at most {QUESTION_TIMEOUT_DEFAULT_SEC}s, then closes the card and "
                "returns TIMEOUT - not a refusal: decide yourself and state the assumption, or "
                "re-ask with mode 'blocking'. "
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
                    "timeout_sec": {"type": "integer",
                                    "description": "timed mode wait bound; the card closes at it."},
                },
                "required": ["requirement", "question"],
            },
        },
    }


async def ask_user_question(question: str, options: Optional[list] = None,
                            context: str = "", mode: str = "blocking",
                            timeout_sec: int = 0, requirement: str = "",
                            *, ctx: Optional[Dict[str, Any]] = None) -> str:
    """Ask the user a question on a card. Returns the answer, or TIMEOUT in timed mode.

    *ctx* is the per-call context: {conversation_id, websocket, channel, session_timer}.
    A missing channel or websocket means there is no live UI to ask - fail closed."""
    ctx = ctx or {}
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
    if state.is_allowed_till_task_done("question"):
        # An earlier card unlocked questions of this kind: asking again would show a card
        # the user already said they do not want. State the choice instead of asking.
        return ("USER DECISION: you may decide questions of this kind yourself for the rest "
                "of this task. State each choice you make in your final answer.")

    opts, dropped, off_topic = normalize_options(options, requirement)
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
                # Nothing can deliver a late answer to a coroutine that already returned, so
                # the card is withdrawn at its deadline instead of staying on screen
                # unanswerable. Fail-closed: the row settles as unanswered.
                if channel is not None:
                    channel.drop_pending_question(request_id)
                    try:
                        await channel.record_runtime("question", request_id, question=question,
                                                     options=[o["label"] for o in opts],
                                                     decision="unanswered")
                    except Exception:
                        pass
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "consent_cancelled", "conv_id": conv_id,
                            "turn_id": getattr(channel, "turn_id", "") or "",
                            "request_id": request_id, "kind": "question"}))
                    except Exception:
                        pass
                return (f"TIMEOUT: the user did not answer within {int(wait)}s and the card is "
                        "closed. This is NOT a refusal. Decide yourself and state the assumption "
                        "you are making, or re-ask with mode 'blocking' when the decision truly "
                        "belongs to the user.")
        else:
            answer = await state.wait_for_decision(request_id, kind="question")
    finally:
        if timer is not None:
            timer.resume()

    if channel is not None:
        channel.drop_pending_question(request_id)
        try:
            # Fold the answer into the SAME row the pending frame opened, so the live frame
            # and the persisted payload carry the same decision.
            await channel.record_runtime("question", request_id, question=question,
                                         options=[o["label"] for o in opts], decision=answer)
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
