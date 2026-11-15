"""
Context-budget diagnostics for the agentic loop (2026-11-07).

WHY THIS EXISTS
    The context guardian fired repeatedly ("97%, injecting finish-current-work")
    while the UI status bar still showed ~100K tokens free, and the trimmer never
    logged a single removal - so the loop was stuck at the limit with NOTHING
    removable. The one-line guardian log could not say WHY: which messages held
    the tokens, how much the tool schema cost, how big the server overhead was,
    or which protected block could not be trimmed.

    This module builds ONE extensive multi-line report for every budget stage
    (safe-margin warning / guardian / hard trim), so a run that hits the big
    limit can be diagnosed from the log alone.

CONTRACT
    Pure text building - NO mutation of messages, NO trimming, NO provider call.
    Every helper is defensive: a bad message dict degrades to a placeholder line
    instead of raising. Logging must never break the loop.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from utils.history_manager import estimate_message_tokens

logger = logging.getLogger("COOLEMS.ContextReport")

#: Notices the loop injects itself - counted separately so "user messages" does not
#: hide the guardian/resume/force-completion text that grew the context.
_NOTICE_MARKERS = ("[SYSTEM NOTICE]", "[USER STEERING MESSAGE", "[USER MESSAGE WHILE YOU WERE WORKING")

#: How many message contributors to list per report (newest cost first).
_TOP_N = 12
#: Preview length per contributor line.
_PREVIEW_CHARS = 110


def _preview(text: Any, limit: int = _PREVIEW_CHARS) -> str:
    """One-line preview of a message body, safe for a log line."""
    if text is None:
        return "<none>"
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)
    flat = text.replace("\r", " ").replace("\n", "\\n")
    if len(flat) > limit:
        flat = flat[:limit] + f"... (+{len(text) - limit} chars)"
    return flat


def _msg_tokens(msg: Dict) -> int:
    try:
        return int(estimate_message_tokens(msg))
    except Exception:
        return 0


def _msg_chars(msg: Dict) -> int:
    content = msg.get("content")
    if isinstance(content, str):
        return len(content)
    if content is None:
        return 0
    try:
        return len(json.dumps(content, ensure_ascii=False))
    except Exception:
        return len(str(content))


def _tool_names(msg: Dict) -> str:
    """Tool names carried by an assistant tool_calls message, or the tool name a
    role='tool' result belongs to (resolved from the preceding assistant call)."""
    calls = msg.get("tool_calls")
    if isinstance(calls, list) and calls:
        names = []
        for c in calls:
            if isinstance(c, dict):
                fn = c.get("function")
                if isinstance(fn, dict):
                    names.append(str(fn.get("name", "?")))
        return ",".join(names) if names else "?"
    if msg.get("role") == "tool":
        return f"result for {msg.get('tool_call_id', '?')}"
    return ""


def role_histogram(messages: List[Dict]) -> str:
    """Per-role message counts + token totals."""
    counts: Dict[str, int] = {}
    tokens: Dict[str, int] = {}
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "?"))
        counts[role] = counts.get(role, 0) + 1
        tokens[role] = tokens.get(role, 0) + _msg_tokens(m)
    parts = [f"{r}={counts[r]}msg/{tokens[r]}tok" for r in sorted(counts)]
    return " | ".join(parts) if parts else "(empty)"


def protected_breakdown(messages: List[Dict]) -> str:
    """The block the trimmer can NEVER remove - the usual cause of a stuck limit.

    Index 0 (system prompt), any `_protect` message (current user prompt), and any
    index the caller passed in `protected_ids` are unremovable. Since 2026-11-08 the
    CURRENT LOOP (everything appended after the protected prompt) is unremovable too -
    it is the AI's working context. When these alone exceed the budget,
    safe_trim_history() removes nothing and the guardian fires - this block is what proves it.
    """
    lines = []
    total = 0
    loop_start = None
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("_protect") is True:
            loop_start = i + 1
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        keep = i == 0 or m.get("_protect") is True
        if not keep:
            continue
        t = _msg_tokens(m)
        total += t
        tag = "system" if i == 0 else "protected user prompt"
        lines.append(f"  idx {i} [{tag}] {t} tok, {len(_preview(m.get('content'), 60))} chars: {_preview(m.get('content'), 60)}")
    if loop_start is not None and len(messages) > loop_start:
        loop_tokens = sum(_msg_tokens(m) for m in messages[loop_start:])
        total += loop_tokens
        lines.append(f"  idx {loop_start}..{len(messages) - 1} [CURRENT LOOP] "
                    f"{len(messages) - loop_start} msgs / {loop_tokens} tok - never trimmed")
    head = f"  unremovable total = {total} tok"
    return head + ("\n" + "\n".join(lines) if lines else "  (none found)")


def top_contributors(messages: List[Dict], n: int = _TOP_N) -> str:
    """The n most expensive messages, with role, tool, size and a preview."""
    rows = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        rows.append((_msg_tokens(m), i, m))
    rows.sort(key=lambda r: -r[0])
    # Everything after the protected prompt is the CURRENT LOOP - never trimmable (2026-11-08).
    loop_start = None
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("_protect") is True:
            loop_start = i + 1
    out = []
    for t, i, m in rows[:n]:
        note = _tool_names(m)
        if i == 0 or m.get("_protect") is True:
            flag = " [UNREMOVABLE]"
        elif loop_start is not None and i >= loop_start:
            flag = " [CURRENT LOOP - never trimmed]"
        else:
            flag = ""
        out.append(f"  #{i} {m.get('role','?')}: {t} tok, {_msg_chars(m)} chars"
                   + (f" ({note})" if note else "") + flag
                   + f" :: {_preview(m.get('content'))}")
    return "\n".join(out) if out else "  (no messages)"


def notice_inventory(messages: List[Dict]) -> str:
    """Loop-injected notices (guardian / resume / force-completion / steering).

    These are role='user' messages the model never asked for. Repeated guardian
    notices inflate the very budget they are complaining about, so they are listed
    separately and totalled.
    """
    found = []
    total = 0
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        if any(content.startswith(k) for k in _NOTICE_MARKERS):
            t = _msg_tokens(m)
            total += t
            found.append(f"  idx {i} {t} tok :: {_preview(content, 80)}")
    if not found:
        return "  injected notices: none"
    return f"  injected notices: {len(found)} = {total} tok\n" + "\n".join(found)


def schema_breakdown(tools: Optional[List[Dict]], n: int = 8) -> str:
    """Tool-schema cost, split per tool.

    get_token_count() does NOT count the schema, so it is added separately to every
    budget figure. A bloated schema can eat the window while the message list looks
    small - per-tool numbers make that visible.
    """
    if not tools:
        return "  tool schema: none sent"
    sized = []
    for t in tools:
        try:
            sized.append((len(json.dumps(t)) // 4, str((t.get("function") or {}).get("name", "?"))))
        except Exception:
            sized.append((0, "?"))
    sized.sort(key=lambda r: -r[0])
    total = sum(s for s, _ in sized)
    head = f"  tool schema: {len(tools)} tools = {total} tok"
    top = [f"    {name} = {tok} tok" for tok, name in sized[:n]]
    return head + "\n" + "\n".join(top)


def build_budget_report(stage: str, *,
                        window_tokens: int,
                        token_limit: int,
                        message_budget: int,
                        trim_budget: int,
                        warn_limit: int,
                        guardian_limit: int,
                        local_est: int,
                        current_tokens: int,
                        anchor_overhead: int,
                        anchor_set: bool,
                        est_tools_tokens: int,
                        prev_tokens: Optional[int],
                        messages: List[Dict],
                        tools: Optional[List[Dict]],
                        guardian_injections: int,
                        guardian_max: int,
                        guardian_armed: bool,
                        token_warning_sent: bool) -> str:
    """Full multi-line report of the budget state at one stage.

    `prev_tokens` is the previous iteration's figure, so the log shows the JUMP that
    crossed the limit and (from the contributor list) which tool result caused it.
    """
    pct_window = round(100.0 * current_tokens / window_tokens) if window_tokens else 0
    pct_limit = round(100.0 * current_tokens / token_limit) if token_limit else 0
    delta = (current_tokens - prev_tokens) if prev_tokens is not None else None

    lines = [
        f"[CONTEXT-BUDGET] stage={stage}",
        f"  limits: window={window_tokens} | history_budget={token_limit} "
        f"(={round(100.0*token_limit/window_tokens) if window_tokens else 0}% of window) "
        f"| warn_limit={warn_limit} | guardian_limit={guardian_limit} "
        f"| message_budget={message_budget} | trim_budget={trim_budget}",
        f"  cost: current={current_tokens} ({pct_window}% of window, {pct_limit}% of history budget) "
        f"| local_est={local_est} | anchor_overhead={anchor_overhead} "
        f"({'server-truth set' if anchor_set else 'NO server anchor yet - estimate only'}) "
        f"| tool_schema={est_tools_tokens}",
        f"  free: window_left={max(0, window_tokens - current_tokens)} "
        f"| history_left={max(0, token_limit - current_tokens)} "
        f"| guardian_margin={max(0, guardian_limit - current_tokens)}",
        f"  movement: prev={prev_tokens if prev_tokens is not None else 'n/a'} "
        f"| delta={delta if delta is not None else 'n/a'} tok this iteration",
        f"  guardian: armed={guardian_armed} injected={guardian_injections}/{guardian_max} "
        f"| safe-margin warning sent={token_warning_sent}",
        f"  messages: {len(messages)} | {role_histogram(messages)}",
        protected_breakdown(messages),
        notice_inventory(messages),
        schema_breakdown(tools),
        f"  top {min(_TOP_N, len(messages))} contributors (by estimated tokens):",
        top_contributors(messages),
    ]
    return "\n".join(lines)


def log_budget(logger_obj: Any, stage: str, report: str) -> None:
    """Emit the report at WARNING for real limit stages, DEBUG for the routine pass.

    Defensive by design: diagnostics must never abort an agentic run.
    """
    try:
        logger_obj.warning(report)
    except Exception as exc:  # pragma: no cover - logging must never break the loop
        logger.debug(f"[CONTEXT-BUDGET] report emission failed: {exc}")
