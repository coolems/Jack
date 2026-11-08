"""
Tool execution logic for agentic mode.

Handles:
- ToolExecutor class with permission checking and memory management
- Silent mode for tools that handle their own UI (e.g., transcribe_image)
- Supports both sync (local) and async orchestrator execute_tool methods

FIXED (2026-08): Sanitizes image base64 data from tool results before adding to history.
  Prevents context window explosion when tools return large images (~939K tokens per screenshot).
"""

import json
import logging
import asyncio
import inspect
import re
import time
import uuid
from typing import Dict, Any, Tuple

from config import MODEL_NAME, AGENTIC_TOOL_OUTPUT_MAX_CHARS
from app.providers import BaseProvider


logger = logging.getLogger("COOLEMS.Logic.ToolExecutor")


# ============================================================
# LEARNED LESSONS - DISABLED ON PURPOSE (dead code kept on purpose).
# Will be fixed later: we are coming back to this. For now the per-tool lessons
# lookup below is skipped so it neither feeds the agentic loop nor complicates
# history trimming. The original logic stays in place as dead code - re-enable
# by setting _LESSONS_INJECTION_ENABLED = True.
# TODO(code-review C7, plan_20261102_2135_code_review.md): re-enable this with a
# lessons payload that is token-budgeted (it must fit inside react_loop's
# message_budget) and trimmed-safe, then delete the disabled branches.
# ============================================================
_LESSONS_INJECTION_ENABLED = False


def _sanitize_image_result(result_text: str) -> str:
    """Sanitize tool result by replacing base64 image data with lightweight reference.

    Tools like full_desktop_screenshot, browser_screenshot return dicts containing
    'image_base64' keys with massive base64 strings (2-5MB). When converted to string
    and added to conversation history, these consume ~939K tokens per image --
    completely destroying the context window.

    CRITICAL: Python str(dict) uses SINGLE quotes ('), not double quotes (").
              json.dumps() uses DOUBLE quotes (").
    The regex must match BOTH formats since tool results may come as either.

    Uses manual string scanning to properly consume the ENTIRE key:value pair including
    all base64 characters, not just the key name.

    Args:
        result_text: String representation of tool result (may contain base64).

    Returns:
        Sanitized string with base64 data replaced by references.
    """
    BASE64_THRESHOLD = 10000

    # Pattern finds: 'image_base64': ' or "image_base64": "
    # Group 1 = key name (e.g., image_base64)
    # Group 2 = the quote character used for values (' or ")
    pattern = re.compile(r"""['"](\w*base64\w*)['"]\s*:\s*(["'])""")

    parts = []
    last_end = 0

    for match in pattern.finditer(result_text):
        key = match.group(1)
        quote_char = match.group(2)  # ' or " -- the quote used for values

        # Find the closing quote of the base64 value
        value_start = match.end()  # First char AFTER opening quote
        pos = value_start

        while pos < len(result_text):
            if result_text[pos] == '\\' and pos + 1 < len(result_text):
                pos += 2
                continue
            if result_text[pos] == quote_char:
                break
            pos += 1

        base64_value = result_text[value_start:pos]

        if len(base64_value) >= BASE64_THRESHOLD:
            # Extract filename from context using simple string search
            import os
            filename = "image"

            for search_key in ['screenshot_path', 'filepath', 'path']:
                idx = result_text.find(search_key)
                if idx >= 0:
                    colon_idx = result_text.find(':', idx)
                    if colon_idx >= 0:
                        val_start = colon_idx + 1
                        while val_start < len(result_text) and result_text[val_start] in " \t'\"":
                            val_start += 1
                        pq = result_text[val_start - 1] if val_start > colon_idx else "'"
                        val_end = result_text.find(pq, val_start)
                        if val_end >= 0:
                            path_val = result_text[val_start:val_end]
                            filename = os.path.basename(path_val)
                            break

            # Build replacement -- replace from match.start() to pos+1 (closing quote inclusive)
            parts.append(result_text[last_end:match.start()])
            replacement = f"{quote_char}{key}{quote_char}: {quote_char}[image: {filename} ({len(base64_value):,} chars sanitized)]{quote_char}"
            parts.append(replacement)
            last_end = pos + 1

    if not parts:
        return result_text  # No replacements made

    parts.append(result_text[last_end:])  # Everything after last replacement
    return ''.join(parts)


# ============================================================
# IMAGE-TO-UI PUSH (2026-09-11)
# Tools that PRODUCE a viewable image (generate_image today; video
# generator later) must render it in the chat INSTANTLY, independent of
# whatever final text the AI writes. The UI renderer already exists
# (websocket.js appendGeneratedImage <- 'image_generated' frame); this is
# the missing call that emits one such frame per successful image.
# Screenshots are deliberately NOT here -- they feed the model, not a card.
# ============================================================
IMAGE_TOOLS = frozenset({"generate_image"})


def result_is_error(result: Any) -> bool:
    """Decide whether a tool result counts as a FAILED call for the cycle history.

    Tools report failures with a leading `ERROR:` / `Error:` / `error:` marker (the
    orchestrator's permission block, tool-level errors, sandbox refusals). Repo-wide the
    LOWERCASE form is by far the most common (96 sites vs 3 uppercase), so matching only
    `ERROR:` recorded genuine failures as `ok: true` - the turn block would have shown a
    red-free summary for a run that actually failed.

    Anything else - including None and a long multi-line report that merely mentions the
    word somewhere in the body - is a successful call, so the ok flag stays honest without
    parsing every tool's wording. Only the HEAD of the result is inspected for that reason.
    """
    if not isinstance(result, str):
        return False
    head = result.lstrip()
    if not head:
        return False
    lowered = head[:12].lower()
    return lowered.startswith("error:") or lowered.startswith("error ")


# ============================================================
# URL CONSENT GATE (2026-07-15) - browser tools pointing at local targets.
# The SSRF guard hard-blocks file:// and loopback URLs; those two categories are NOT
# SSRF risks, so instead of a silent block the AI asks the LOCAL USER via the same
# approval card as python_exec (allow / deny / "until task done"). On Allow, a ONE-SHOT
# exact-match grant is registered in tools.ssrf_guard and this exact URL passes.
# Only these URL-bearing navigation/fetch tools are gated; everything else (private LAN,
# metadata IPs, CGNAT, foreign file:// paths) stays hard-blocked and is never asked.
# ============================================================
URL_CONSENT_TOOLS = frozenset({"goto", "check_url", "download_file"})


def _extract_image_payloads(tool_result):
    """Pull viewable-image payloads out of a tool result (dict or list/tuple).

    A payload qualifies when it is a dict with status == 'ok' and an
    'image_base64' string >= 100 chars. Returns a list of dicts shaped for the
    'image_generated' UI frame: image_base64, filename, file_path, width, height.
    Non-image tools / error results (image_base64 is None) yield [].
    """
    payloads = []
    if tool_result is None:
        return payloads

    candidates = []
    if isinstance(tool_result, dict):
        candidates.append(tool_result)
    elif isinstance(tool_result, (list, tuple)):
        for item in tool_result:
            if isinstance(item, dict):
                candidates.append(item)

    for cand in candidates:
        b64 = cand.get("image_base64")
        if not (isinstance(b64, str) and len(b64) >= 100):
            continue
        if cand.get("status", "ok") != "ok":
            continue
        # (2026-10-04) Carry the per-run setup/tier report too: generate_image attaches a
        # compact 'setup' dict to EVERY successful result (tier used, GPU, model line, what
        # was installed/downloaded this run - or an honest "already cached" note). Without it
        # in the frame, a fully-cached machine shows NO setup/tier info at all in chat.
        payload = {
            "image_base64": b64,
            "filename": cand.get("filename"),
            "file_path": cand.get("file_path"),
            "width": cand.get("width"),
            "height": cand.get("height"),
        }
        for _k in ("setup", "pipeline", "model_id", "dtype"):
            if isinstance(cand.get(_k), (dict, str)):
                payload[_k] = cand.get(_k)
        payloads.append(payload)
    return payloads


# ============================================================
# DESKTOP CONSENT GATE (2026-11-07) - Computer Use tools (tools/computer_use/*).
# The AI thinks on a remote SERVER, but these tools read and drive applications on
# THIS machine. Every app-bound call therefore goes through the shared consent dialog
# (kind 'desktop') BEFORE execution, with the APP IDENTITY as the payload - the user
# approves "may this AI drive that app?", not code.

# The gate is CLIENT-side; the tool files ALSO verify the grant themselves (fail-closed),
# so no path can bypass it: the CLIENT records the decision in tools._consent (the dep's
# store, reached through sys.modules['tools._consent']) and the tool re-reads it.
# cua_capabilities is NOT gated - it is read-only preflight that grants nothing.
# ============================================================
from .tool_lock import DESKTOP_CONSENT_TOOLS

#: One-shot enumeration grant key used by cua_list_apps (window titles are app data).
DESKTOP_ENUMERATE_KEY = "desktop:enumerate"


def _desktop_payload(tool_name: str, tool_args: Dict) -> tuple:
    """(app_identity, description) shown on the desktop consent card.

    The identity comes from tools._consent.call_identity(), which resolves handles,
    windows and snapshots to the SAME app the tool will verify the grant against - so the
    card can never name an app different from the one the Allow covers.
    """
    store = _desktop_consent_store()
    identity = ""
    if store is not None:
        try:
            identity = store.call_identity(tool_name, tool_args) or ""
        except Exception:
            identity = ""
    if not identity:
        identity = "the desktop (no identifiable app)"

    action = {
        "cua_get_app": "bind an app handle and read its windows",
        "cua_list_apps": "list every running app (window titles)",
        "cua_list_windows": "read this app's window list and rectangles",
        "cua_get_state": "read this app's UI control tree",
        "cua_screenshot": "capture this app's window as an image",
        "cua_click": "click in this app's window",
        "cua_hover": "move the pointer over this app's window",
        "cua_drag": "drag inside this app's window",
        "cua_scroll": "scroll inside this app's window",
        "cua_type_text": "type text into this app's focused field",
        "cua_press_key": "send a key chord to this app",
        "cua_paste": "paste text into this app (the clipboard is restored afterwards)",
        "cua_perform_action": "run a UI action on one of this app's controls",
        "cua_select_text": "select text inside this app's field",
        "cua_show_window": "unhide/unminimize this app's window (no focus steal)",
    }.get(tool_name, tool_name)

    description = (f"The AI wants to {action} on: {identity}. Nothing is launched, activated, "
                   "closed or killed, and a delivered action is never treated as proof. Deny to "
                   "block this app for the rest of the session.")
    return identity, description


def _desktop_consent_store():
    """The tools._consent store (the single source of desktop grants), or None.

    install_shared_deps() registers each same-package dep as sys.modules['tools.<dep>'],
    so the CLIENT reaches the SAME object the tool files read - one store, two writers.
    """
    import sys as _sys
    mod = _sys.modules.get("tools._consent")
    if mod is None:
        try:
            import tools._consent as _c  # local SERVER package path (non-CLIENT runs)
            mod = _c
        except Exception:
            mod = None
    return mod


class _DesktopConsent:
    """Outcome of one desktop consent ask (mirrors ConsentResult's shape)."""

    def __init__(self, decision: str, request_id: str = "", explain_instruction: str = ""):
        self.decision = decision
        self.request_id = request_id
        self.explain_instruction = explain_instruction

    @property
    def allowed(self) -> bool:
        return self.decision in ("allow", "allow_category_until_done")


class ToolExecutor:
    """Wrapper for executing tools with proper context and permission checking.

    Supports both:
      - Sync orchestrators (current ToolOrchestrator) -- execute_tool() returns str directly
      - Async orchestrators (RemoteToolOrchestrator) -- execute_tool() is coroutine, may fetch from SERVER
    """

    def __init__(self, orchestrator: Any, lesson_manager: Any = None,
                 provider: BaseProvider = None,
                 current_model: str = MODEL_NAME,
                 api_key: str = None,
                 session_timer=None,  # (2026-08-23) active-time timer; paused while user menus are open
                 cycle=None):  # (2026-11-07) app.cycle_recorder.CycleRecorder for THIS turn
        self.orchestrator = orchestrator
        self.lesson_manager = lesson_manager
        self.provider = provider
        self.current_model = current_model
        self.api_key = api_key
        self.session_timer = session_timer  # may be None (non-bus / legacy callers)
        # The turn's live history recorder (None for legacy callers). Every recorder call
        # goes through _rec(), which swallows errors: history is best-effort and must never
        # break a tool call.
        self.cycle = cycle

        # Detect if orchestrator uses async execute_tool (RemoteToolOrchestrator)
        self._is_async_orchestrator = inspect.iscoroutinefunction(
            getattr(self.orchestrator, 'execute_tool', None)
        )

    # ------------------------------------------------------------------
    # Cycle-history hooks (2026-11-07) - best-effort, never raise
    # ------------------------------------------------------------------

    async def _rec(self, method: str, *args, **kwargs) -> None:
        """Call one CycleRecorder method, swallowing every error.

        History is a display feature: a dead recorder, a dead socket or a recorder
        without the method must NEVER stop an approved tool call.
        """
        rec = getattr(self, "cycle", None)
        if rec is None:
            return
        fn = getattr(rec, method, None)
        if fn is None:
            return
        try:
            await fn(*args, **kwargs)
        except Exception as exc:
            logger.debug("[CYCLE] recorder.%s failed (non-fatal): %r", method, exc)

    async def _record_consent_pending(self, request_id: str, payload: Any,
                                      kind: str = "python", description: str = "") -> None:
        """Open the consent row when a card actually appears on screen."""
        await self._rec("consent_pending", kind, request_id or "", payload, description)

    async def _record_consent_decision(self, request_id: str, decision: str,
                                       kind: str = "python", payload: Any = "") -> None:
        """Fold the card's answer into its row (the card itself is transient).

        A row is folded when a card actually appeared (a real request_id) OR when the
        answer is NOT an auto-allow: 'deny' / 'explain' / 'unavailable' mean the user saw
        a card or the ask could not be answered at all - both belong in the history. The
        auto-allow paths ('allow' / 'allow_category_until_done' from a per-kind grant)
        answer without a card, so they add no consent row; the grant row records them
        once per kind (deduped in TurnLog).
        """
        if request_id or decision not in ("allow", "allow_category_until_done"):
            await self._rec("consent", kind, decision, request_id, payload)
        if decision == "allow_category_until_done":
            await self._rec("grant", kind, request_id)

    async def _record_tool(self, name: str, args: Any, ok: bool,
                           output: Any, duration_ms: float, call_id: str = "") -> None:
        """Record one tool call - including calls the user declined (they happened, nothing ran)."""
        await self._rec("tool", name, args, bool(ok), output, float(duration_ms or 0.0),
                        call_id=call_id)

    async def execute(
        self,
        tool_name: str,
        tool_args: Dict,
        conversation_history: list,
        websocket: Any,
        silent: bool = False,
        live_messages: list = None,
        conversation_id: str = None
    ) -> Tuple[str, str]:
        """
        Execute a tool with permission checking.

        live_messages (2026-08-24): the ReAct loop's LIVE message list. When a tool
        result carries image_base64 (view_image / screenshots), those pixels are
        attached to the protected current-user prompt in *live_messages* so the NEXT
        provider call actually sees them. Falls back to conversation_history when
        live_messages is not provided.
        conversation_id (2026-09-24): identifies this chat's ChatBus channel so a
        python_exec call can be gated on the local user's approval ('allow' / 'deny'
        / 'allow_category_until_done' = run until task done). See _ask_user_for_python_exec().


        Returns:
            Tuple of (tool_result, agent_lessons)
        """
        exec_start = time.time()
        # (2026-11-08 loop history) One id per execution. `tool_start`, `tool_end` and the
        # recorded cycle row all carry it, so the UI's LIVE per-tool timer (started on
        # tool_start, stopped on the row) always belongs to THIS call - never to another
        # tool of the same batch or another workspace. The conversation prefix makes the id
        # workspace-scoped, so a timer map keyed by call_id cannot collide across chats.
        call_id = f"{(conversation_id or '')[:8]}:{uuid.uuid4().hex[:8]}"

        from app.keys import get_key_role
        
        from config import DEFAULT_ROLE_NO_KEY
        
        role = get_key_role(self.api_key) if self.api_key else DEFAULT_ROLE_NO_KEY
        logger.debug(f"[AGENTIC.DEBUG] ToolExecutor.execute() - Key role resolved: '{role}'")

        # ============================================================
        # USER APPROVAL GATE for python_exec (2026-09-24)
        # The AI runs on the SERVER, but python_exec executes code ON THIS machine.
        # Every call is shown to the local user in the chat with Allow / Deny /
        # "Allow until task done" buttons; only an explicit YES reaches the orchestrator.
        # A NO (or an unavailable approval channel) returns a message telling the AI that the
        # user decided not to run this code - nothing is executed, ever.
        # ============================================================
        if tool_name == "python_exec":
            _consent = await self._ask_user_for_python_exec(tool_args, websocket, conversation_id)
            if _consent.allowed:
                # The card appeared and the user answered it - record that answer (the
                # helper skips auto-allow answers that never showed a card).
                await self._record_consent_decision(
                    getattr(_consent, "request_id", ""), _consent.decision, "python",
                    (tool_args or {}).get("code", "") if isinstance(tool_args, dict) else "")
                # approved (or auto-run active for this task) -> fall through and execute
            elif _consent.decision == "explain":
                logger.info("[EXEC-APPROVAL] python_exec EXPLAIN requested - nothing executed, AI will explain + re-ask")
                await self._record_consent_decision(
                    getattr(_consent, "request_id", ""), "explain", "python",
                    (tool_args or {}).get("code", "") if isinstance(tool_args, dict) else "")
                return _consent.explain_instruction, ""
            else:
                logger.warning("[EXEC-APPROVAL] python_exec NOT executed - user declined (or approval unavailable)")
                deny_msg = ("USER DECISION: You decided to not execute this python code. The code was NOT executed and must "
                             "not be re-submitted or worked around in any form. Stop the current approach and respond to the user "
                             "with something like: \"You decided to not execute this python code. Now you can instruct me in a new "
                             "direction.\" Also include your continuation plan <plan> - the remaining steps of the current task so far, "
                             "clearly numbered - in case the user wants to resume it later.")
                # The card happened even though nothing ran: record the decision AND a failed
                # tool call, so the turn block shows the declined attempt instead of silence.
                await self._record_consent_decision(
                    getattr(_consent, "request_id", ""), _consent.decision, "python",
                    (tool_args or {}).get("code", "") if isinstance(tool_args, dict) else "")
                await self._record_tool(tool_name, tool_args, False, "NOT executed - user declined",
                                        (time.time() - exec_start) * 1000.0, call_id=call_id)
                return deny_msg, ""

        # ============================================================
        # USER APPROVAL GATE for URL-bearing browser tools (2026-07-15, extended 2026-10-23).
        # Pre-checks the SSRF guard BEFORE execution: when a blocked target is
        # CONSENTABLE (file:// inside working root, loopback http(s), private/reserved
        # IP targets) it asks the local user - the card names the exact target and its
        # class; on Allow it registers the one-shot grant so the tool's own
        # ensure_url_not_ssrff() passes for this exact URL. Unclassifiable targets and
        # a declined question fail closed with an explanatory message (no execution).
        # ============================================================
        if tool_name in URL_CONSENT_TOOLS:
            url_arg = (tool_args or {}).get("url") if isinstance(tool_args, dict) else None
            if isinstance(url_arg, str) and url_arg.strip():
                _url_consent = await self._maybe_ask_url_consent(url_arg, websocket, conversation_id)
                if _url_consent.allowed:
                    # (2026-11-08) The card must DISAPPEAR once answered - including when the
                    # auto-approve timer fired. The UI removes the card on the cycle_consent
                    # frame, so an ALLOWED url consent has to be recorded here exactly like the
                    # python/desktop gates do; without it the card stayed on screen after the
                    # timer auto-allowed the target.
                    await self._record_consent_decision(
                        getattr(_url_consent, "request_id", ""), _url_consent.decision, "url", url_arg)
                elif _url_consent.decision == "explain":
                    logger.info("[URL-CONSENT] EXPLAIN requested - nothing opened, AI will explain + re-ask")
                    await self._record_consent_decision(
                        getattr(_url_consent, "request_id", ""), "explain", "url", url_arg)
                    return _url_consent.explain_instruction, ""
                else:
                    logger.warning("[URL-CONSENT] %s NOT executed - blocked target declined or unconsentable", tool_name)
                    deny_msg = ("USER DECISION: This URL was refused by the local user's browser-security gate. "
                                f"The navigation to {url_arg[:200]} did NOT happen and must not be retried with the same URL. "
                                "Tell the user they can click Allow on the approval card (or ask them again in a new task). "
                                "Otherwise use an alternative approach: serve the file over http://localhost, read it as text "
                                "with the file tools, or explain what you intended.")
                    await self._record_consent_decision(
                        getattr(_url_consent, "request_id", ""), _url_consent.decision, "url", url_arg)
                    await self._record_tool(tool_name, tool_args, False, "NOT executed - URL refused",
                                            (time.time() - exec_start) * 1000.0, call_id=call_id)
                    return deny_msg, ""

        # ============================================================
        # USER APPROVAL GATE for Computer Use tools (2026-11-07).
        # The payload is an APP IDENTITY, so the card asks "may this AI drive that
        # application?". Denied/explained calls never reach the orchestrator; the tool
        # files re-verify the grant themselves, so this gate cannot be bypassed.
        # ============================================================
        if tool_name in DESKTOP_CONSENT_TOOLS:
            _desktop = await self._maybe_ask_desktop_consent(tool_name, tool_args,
                                                             websocket, conversation_id)
            if _desktop.allowed:
                await self._record_consent_decision(_desktop.request_id, _desktop.decision,
                                                    "desktop", _desktop_payload(tool_name, tool_args)[0])
            elif _desktop.decision == "explain":
                logger.info("[DESKTOP-CONSENT] %s EXPLAIN requested - nothing touched, AI will explain + re-ask", tool_name)
                await self._record_consent_decision(_desktop.request_id, "explain", "desktop",
                                                    _desktop_payload(tool_name, tool_args)[0])
                return _desktop.explain_instruction, ""
            else:
                logger.warning("[DESKTOP-CONSENT] %s NOT executed - app access refused (%s)",
                               tool_name, _desktop.decision)
                identity = _desktop_payload(tool_name, tool_args)[0]
                deny_msg = ("USER DECISION: Desktop access was refused. This call did NOT read or "
                            f"touch anything on the machine ({identity}) and must not be retried or "
                            "worked around with another tool. Tell the user they can Allow the app on "
                            "the approval card, or ask them again in a new task. If they want the "
                            "information another way, offer a non-desktop approach (file tools, "
                            "browser tools, or just explain what you intended).")
                await self._record_consent_decision(_desktop.request_id, _desktop.decision, "desktop", identity)
                await self._record_tool(tool_name, tool_args, False, "NOT executed - desktop access refused",
                                        (time.time() - exec_start) * 1000.0, call_id=call_id)
                return deny_msg, ""

        # Send tool start notification (skip if silent mode)
        if not silent:
            logger.debug(f"[AGENTIC.DEBUG] ToolExecutor.execute() - Sending tool_start notification for '{tool_name}'")
            await websocket.send_text(json.dumps({
                "type": "tool_start",
                "tool": tool_name,
                "input": str(tool_args),
                "call_id": call_id,
                # Wall-clock start of THIS call - the UI ticks the per-tool timer from it,
                # so the duration shown is the real execution time, not a frame arrival time.
                "started_at": exec_start,
                "conv_id": conversation_id or "",
            }))

        # Execute the tool -- handle both sync and async orchestrators
        # `cycle` travels with the call context so SERVER-side tools (and the python_exec
        # sandbox relay) can record their OWN consent cards on this turn's recorder.
        if self._is_async_orchestrator:
            # RemoteToolOrchestrator -- execute_tool is a coroutine (may fetch from SERVER)
            tool_result = await self.orchestrator.execute_tool(
                tool_name,
                tool_args,
                conversation_history=conversation_history,
                api_key=self.api_key,
                websocket=websocket,
                conversation_id=conversation_id,
                cycle=self.cycle
            )
        elif tool_name == 'generate_image':
            # Sync orchestrator + blocking subprocess -- run in thread
            tool_result = await asyncio.to_thread(
                self.orchestrator.execute_tool,
                tool_name,
                tool_args,
                conversation_history=conversation_history,
                api_key=self.api_key
            )
            logger.debug("[AGENTIC.DEBUG] ToolExecutor.execute() - generate_image thread complete")
        else:
            # Sync orchestrator -- call directly
            tool_result = self.orchestrator.execute_tool(
                tool_name,
                tool_args,
                conversation_history=conversation_history,
                api_key=self.api_key,
                websocket=websocket,
                conversation_id=conversation_id,
                cycle=self.cycle
            )

        exec_elapsed = time.time() - exec_start

        # (2026-08-24) EXTRACT TOOL-VIEWED IMAGES BEFORE SANITIZATION: tools like
        # view_image / screenshots return a dict containing 'image_base64'. The string
        # form gets sanitized out of history below (to protect the context window), so
        # without this hook the pixels would NEVER reach the model - only the filename.
        # We pull them out here and attach them to the protected current-user prompt,
        # where the NEXT provider call carries real pixels exactly once.
        _pending_images: list = []
        if isinstance(tool_result, dict):
            for _key in ("image_base64", "screenshot_base64"):
                _val = tool_result.get(_key)
                if isinstance(_val, str) and len(_val) >= 100:
                    _pending_images.append(_val)
        elif isinstance(tool_result, (list, tuple)):
            for _item in tool_result:
                if isinstance(_item, dict):
                    for _key in ("image_base64", "screenshot_base64"):
                        _val = _item.get(_key)
                        if isinstance(_val, str) and len(_val) >= 100:
                            _pending_images.append(_val)

        # Convert result to string for processing
        result_str = str(tool_result) if not isinstance(tool_result, str) else tool_result

        # CRITICAL FIX: Sanitize image base64 data from tool results before adding to history.
        # Without this, a single screenshot (~3.75MB base64) would consume ~939K tokens,
        # completely destroying the 131K context window.
        original_len = len(result_str)
        result_str = _sanitize_image_result(result_str)
        sanitized_savings = original_len - len(result_str)

        if sanitized_savings > 0:
            saved_tokens = max(1, round(sanitized_savings / 4.0))
            logger.info(
                f"[AGENTIC.DEBUG] ToolExecutor.execute() - Sanitized image data from '{tool_name}': "
                f"{original_len:,} -> {len(result_str):,} chars ({saved_tokens:,} tokens saved)"
            )

        # Check if tool was blocked due to permissions
        if isinstance(result_str, str) and result_str.startswith("ERROR: Tool"):
            logger.warning(f"[AGENTIC.DEBUG] Tool '{tool_name}' BLOCKED for role '{role}'")
            await websocket.send_text(json.dumps({
                "type": "tool_end",
                "tool": tool_name,
                "output": result_str[:AGENTIC_TOOL_OUTPUT_MAX_CHARS],
                "call_id": call_id,
                "duration_ms": round((time.time() - exec_start) * 1000.0, 1),
                "conv_id": conversation_id or "",
            }))
            # The call happened and failed: record it so the turn block shows the refusal.
            await self._record_tool(tool_name, tool_args, False, result_str,
                                    (time.time() - exec_start) * 1000.0, call_id=call_id)
            return result_str, ""

        # Send tool end notification (skip if silent mode)
        if not silent:
            logger.debug(f"[AGENTIC.DEBUG] ToolExecutor.execute() - Sending tool_end notification for '{tool_name}'")
            await websocket.send_text(json.dumps({
                "type": "tool_end",
                "tool": tool_name,
                "output": result_str[:AGENTIC_TOOL_OUTPUT_MAX_CHARS],
                "call_id": call_id,
                "duration_ms": round((time.time() - exec_start) * 1000.0, 1),
                "conv_id": conversation_id or "",
            }))

        # (2026-09-11) IMAGE-TO-UI PUSH: content-producing tools must render their
        # image in the chat INSTANTLY -- independent of whatever final text the AI
        # writes at the end of the task. The UI renderer already exists (websocket.js
        # appendGeneratedImage <- 'image_generated' frame); we were never emitting it.
        # One frame per successful image payload, sent right after tool_end so chat
        # ordering is [tool_start][tool_end][image]. Reads the raw dict (b64 intact).
        if not silent and tool_name in IMAGE_TOOLS:
            for _img in _extract_image_payloads(tool_result):
                try:
                    await websocket.send_text(json.dumps({
                        "type": "image_generated",
                        "image_base64": _img["image_base64"],
                        "file_path": _img.get("file_path"),
                        "filename": _img.get("filename"),
                        "width": _img.get("width"),
                        "height": _img.get("height"),
                        # (2026-11-08 loop history) the image belongs to THIS tool call of
                        # THIS conversation - the block renders it as a row + a thumbnail in
                        # the last-tool strip, and never into another workspace's view.
                        "call_id": call_id,
                        "conv_id": conversation_id or "",
                        # (2026-10-04) always-visible setup/tier report for the image card.
                        "setup": _img.get("setup"),
                        "pipeline": _img.get("pipeline"),
                        "model_id": _img.get("model_id"),
                        "dtype": _img.get("dtype"),
                    }))
                    logger.info(
                        f"[AGENTIC.DEBUG] ToolExecutor.execute() - Pushed 'image_generated' UI frame for '{tool_name}'"
                    )
                except Exception as _img_err:
                    logger.warning(f"[AGENTIC.DEBUG] Image-to-UI push failed (non-fatal): {_img_err}")

        # Check for lessons for this tool
        # LEARNED LESSONS - skipped on purpose (see _LESSONS_INJECTION_ENABLED at
        # module top). Dead code kept for a later fix: we'll come back to it.
        agent_lessons = ""
        if not _LESSONS_INJECTION_ENABLED:
            pass  # lessons intentionally disabled right now
        elif self.lesson_manager and hasattr(self.lesson_manager, 'lessons_for_tool'):
            agent_lessons = self.lesson_manager.lessons_for_tool(tool_name)

        # Re-attach extracted pixels to the protected current-user message so the next
        # provider call in the ReAct loop actually carries them (dedupe + size cap inside).
        if _pending_images:
            try:
                from .base_mode import attach_images_to_current_user_message
                _attach_target = live_messages if live_messages is not None else (conversation_history or [])
                _added = attach_images_to_current_user_message(_attach_target, _pending_images)

                logger.info(
                    f"[AGENTIC.DEBUG] ToolExecutor.execute() - Re-attached {_added}/{len(_pending_images)} "
                    f"tool image(s) from '{tool_name}' to the current user prompt for next provider call"
                )
            except Exception as _attach_err:
                logger.warning(f"[AGENTIC.DEBUG] Image re-attach failed (non-fatal): {_attach_err}")

        total_elapsed = time.time() - exec_start
        logger.debug(f"[AGENTIC.DEBUG] ToolExecutor.execute() - COMPLETE | tool='{tool_name}' total_time={total_elapsed:.2f}s")

        # Cycle history (2026-11-07): record the call that actually ran, with an honest ok
        # flag (result_is_error reads the tool's own ERROR: prefix). Done last so the row
        # carries the final sanitized output the model saw.
        await self._record_tool(tool_name, tool_args, not result_is_error(result_str),
                                result_str, total_elapsed * 1000.0, call_id=call_id)
        return result_str, agent_lessons
    # ------------------------------------------------------------------
    # python_exec user approval (2026-09-24) - see app/chat_bus/exec_approval.py
    # ------------------------------------------------------------------

    async def _ask_user_for_python_exec(self, tool_args: Dict, websocket: Any,
                                        conversation_id: str = None):
        """Ask the local user whether this python_exec code may run on THIS machine.

        Thin wrapper over the shared consent dialog (app/chat_bus/consent_dialog.py,
        kind='python') - ONE code path for all four card kinds. Returns a ConsentResult:
          allowed=True              -> execute ('allow', or 'allow_category_until_done' grant active)
          decision == 'explain'     -> user clicked Explain request; caller returns the
                                       instruction text (nothing executed)
          anything else             -> fail-closed, do NOT execute

        There is NO timeout on the question: it blocks until the user answers (or the
        turn is stopped). The per-run state lives on the chat's ChatChannel (fresh each
        turn), so a "run until task done" grant lasts exactly one agentic run.
        """
        from app.chat_bus import ConsentDialog, ConsentResult

        code = (tool_args or {}).get("code", "") if isinstance(tool_args, dict) else ""
        if not isinstance(code, str) or not code.strip():
            # Empty/invalid payload: let python_exec() itself answer with its own message.
            return ConsentResult("allow")

        # (2026-07-18 rev4) the one-time allow-until-task-done system message is sent ONCE per
        # agentic run PER KIND: only on the click that first activates THIS kind's grant.
        from app.chat_bus.consent_dialog import get_approval_state as _gas
        from app.chat_bus import get_chat_bus as _gb
        _state = _gas(_gb(), conversation_id)
        _already_granted = bool(getattr(_state, "was_notified", lambda k: False)("python"))

        result = await ConsentDialog("python").ask(
            websocket=websocket, conv_id=conversation_id, payload=code,
            session_timer=self.session_timer)

        if result.decision == "allow_category_until_done" and not _already_granted:
            # Grant is now active for the REST OF THIS RUN - python kind only (per-kind flags, rev4).
            try:
                await websocket.send_text(json.dumps({
                    "type": "system",
                    "content": "Allow-until-task-done enabled: consent requests will be approved without asking until this task ends.",
                }))
            except Exception:
                pass  # dead socket - the grant still holds in state; card shows it client-side
        return result
    # ------------------------------------------------------------------
    # Desktop consent gate for Computer Use tools (2026-11-07) - shared dialog, kind='desktop'
    # ------------------------------------------------------------------

    async def _maybe_ask_desktop_consent(self, tool_name: str, tool_args: Dict,
                                         websocket: Any, conversation_id: str = None):
        """Ask the local user before a Computer Use tool touches an application.

        Fail-closed everywhere: no consent store, no approval state, or an undeliverable
        frame means the call does NOT run. The grant is recorded in the SAME store the
        tool reads (tools._consent), under the exact key the tool will verify - so the
        card, the grant and the tool can never disagree about which app is allowed.

        'Allow until task done' grants the desktop kind only (per-kind flags): later
        desktop calls in THIS run still need the app to be named on the card, but the
        dialog is skipped for an app already granted in this conversation.
        """
        store = _desktop_consent_store()
        if store is None:
            logger.warning("[DESKTOP-CONSENT] tools._consent not installed - failing closed")
            return _DesktopConsent("unavailable")

        conv_id = conversation_id or ""
        try:
            key = store.consent_key_for_call(tool_name, tool_args)
        except Exception as exc:
            logger.warning("[DESKTOP-CONSENT] key derivation failed (%r) - failing closed", exc)
            return _DesktopConsent("unavailable")
        if not key or not conv_id:
            # No identifiable app or no chat to ask through: the tool itself fails closed.
            logger.info("[DESKTOP-CONSENT] %s has no identifiable app / no conversation - failing closed", tool_name)
            return _DesktopConsent("unavailable")

        try:
            if store.was_denied(key, conv_id):
                logger.info("[DESKTOP-CONSENT] %s already denied for this session - not asking again", tool_name)
                return _DesktopConsent("deny")
            if store.call_granted(tool_name, tool_args, conv_id):
                # Already allowed in THIS conversation: no repeat card (their rule).
                logger.info("[DESKTOP-CONSENT] %s allowed (session grant) key=%s", tool_name, key)
                return _DesktopConsent("allow")
        except Exception as exc:
            logger.warning("[DESKTOP-CONSENT] grant lookup failed (%r) - failing closed", exc)
            return _DesktopConsent("unavailable")

        from app.chat_bus import ConsentDialog, explain_instruction

        identity, description = _desktop_payload(tool_name, tool_args)
        result = await ConsentDialog("desktop").ask(
            websocket=websocket, conv_id=conv_id, payload=identity,
            description=description, session_timer=self.session_timer)

        request_id = getattr(result, "request_id", "")
        if result.decision == "explain":
            return _DesktopConsent("explain", request_id, result.explain_instruction)

        if result.allowed:
            run_wide = result.decision == "allow_category_until_done"
            try:
                store.grant_call(tool_name, tool_args, conv_id, run_wide=run_wide)
            except Exception as exc:
                logger.warning("[DESKTOP-CONSENT] grant record failed (%r) - failing closed", exc)
                return _DesktopConsent("unavailable", request_id)
            if run_wide:
                logger.info("[DESKTOP-CONSENT] allow-until-task-done active for desktop kind")
            logger.info("[DESKTOP-CONSENT] %s allowed (%s) app=%s", tool_name, result.decision, identity)
            return _DesktopConsent(result.decision, request_id)

        # deny / unavailable: block this app for the session so it is not re-asked.
        try:
            store.deny_call(tool_name, tool_args, conv_id)
        except Exception as exc:
            logger.debug("[DESKTOP-CONSENT] deny record failed (non-fatal): %r", exc)
        return _DesktopConsent(result.decision, request_id,
                               explain_instruction("desktop", identity) if result.decision == "explain" else "")

    # ------------------------------------------------------------------
    # URL consent gate for browser tools (2026-07-15) - shared dialog, kind='url'
    # ------------------------------------------------------------------

    async def _maybe_ask_url_consent(self, url: str, websocket: Any,
                                     conversation_id: str = None):
        """Decide whether a URL-bearing browser tool may proceed to *url*.

        Returns a ConsentResult. allowed=True when the SSRF guard already accepts the
        URL (normal public web - no user interaction at all) or after an explicit user
        Allow / active run-all grant for a CONSENTABLE target -- in that case a one-shot
        exact-match grant is registered so the tool's own ensure_url_not_ssrff() passes.
        decision=='explain' means the user clicked Explain request (nothing opened;
        caller returns the instruction). Everything else fails closed. Never raises.

        Consentable categories (2026-10-23: extended beyond local-only so the USER
        decides what is ok for him): file:// inside working root, loopback http(s) and
        private/reserved IP targets (LAN / link-local metadata / CGNAT / ...). The card's
        description line names the exact target + class from classify_blocked_url().

        "Allow until task done" on a URL consent card grants the url kind only (2026-07-18 rev4):
            every later consentable target of THIS agentic run passes without asking;
        unclassifiable targets stay hard-blocked even under auto-consent.
        """
        from app.chat_bus import ConsentDialog, ConsentResult

        try:
            from tools.ssrf_guard import validate_url_not_ssrff, classify_blocked_url, grant_url
        except Exception as e:  # guard unavailable -> let the tool's own check answer (it fails closed there)
            logger.warning(f"[URL-CONSENT] ssrf_guard unavailable ({e!r}) - skipping pre-gate")
            return ConsentResult("allow")

        safe, _reason = validate_url_not_ssrff(url)
        if safe:
            return ConsentResult("allow")  # public web target -- normal path, no question asked

        consentable, why = classify_blocked_url(url)
        if not consentable:
            logger.info(f"[URL-CONSENT] hard block (not consentable): {url} - {why}")
            return ConsentResult("deny")

        # (2026-07-18 rev4) system message ONCE per agentic run PER KIND - only on the first
        # "Allow until task done" click for THIS kind.
        from app.chat_bus.consent_dialog import get_approval_state as _gas2
        from app.chat_bus import get_chat_bus as _gb2
        _state2 = _gas2(_gb2(), conversation_id)
        _already_granted = bool(getattr(_state2, "was_notified", lambda k: False)("url"))

        result = await ConsentDialog("url").ask(
            websocket=websocket, conv_id=conversation_id, payload=url,
            description=f"The SSRF guard blocked this target: {why}",
            session_timer=self.session_timer)

        if result.allowed:
            grant_url(url)  # 'allow' = this one now; 'allow_category_until_done' = this one + rest of the run
            # The browser tools auto-prefix 'https://' on scheme-less input -- grant that
            # normalized form too so the tool's own ensure_url_not_ssrff() passes (the
            # ledger is exact-match, 2026-10-23).
            if not re.match(r'^[a-z][a-z0-9+.-]*://', url or '', flags=re.IGNORECASE):
                grant_url('https://' + url)
            logger.info(f"[URL-CONSENT] allowed target ({result.decision}): {url[:120]}")
            if result.decision == "allow_category_until_done" and not _already_granted:
                try:
                    await websocket.send_text(json.dumps({
                        "type": "system",
                        "content": "Auto-open enabled: blocked-but-consentable URL targets (local files, localhost and private/LAN addresses) will open without asking until this task ends.",
                    }))
                except Exception:
                    pass  # dead socket - the grant still holds in state; card shows it client-side

        return result
