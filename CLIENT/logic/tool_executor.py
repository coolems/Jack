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
        payloads.append({
            "image_base64": b64,
            "filename": cand.get("filename"),
            "file_path": cand.get("file_path"),
            "width": cand.get("width"),
            "height": cand.get("height"),
        })
    return payloads


class ToolExecutor:
    """Wrapper for executing tools with proper context and permission checking.

    Supports both:
      - Sync orchestrators (current ToolOrchestrator) -- execute_tool() returns str directly
      - Async orchestrators (RemoteToolOrchestrator) -- execute_tool() is coroutine, may fetch from SERVER
    """

    def __init__(self, orchestrator: Any, lesson_manager: Any = None,
                 provider: BaseProvider = None,
                 current_model: str = MODEL_NAME,
                 api_key: str = None):
        self.orchestrator = orchestrator
        self.lesson_manager = lesson_manager
        self.provider = provider
        self.current_model = current_model
        self.api_key = api_key

        # Detect if orchestrator uses async execute_tool (RemoteToolOrchestrator)
        self._is_async_orchestrator = inspect.iscoroutinefunction(
            getattr(self.orchestrator, 'execute_tool', None)
        )

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
        / 'run_all' = run until task done). See _ask_user_for_python_exec().


        Returns:
            Tuple of (tool_result, agent_lessons)
        """
        exec_start = time.time()

        from app.keys import get_key_role
        
        from config import DEFAULT_ROLE_NO_KEY
        
        role = get_key_role(self.api_key) if self.api_key else DEFAULT_ROLE_NO_KEY
        logger.debug(f"[AGENTIC.DEBUG] ToolExecutor.execute() - Key role resolved: '{role}'")

        # ============================================================
        # USER APPROVAL GATE for python_exec (2026-09-24)
        # The AI runs on the SERVER, but python_exec executes code ON THIS machine.
        # Every call is shown to the local user in the chat with Allow / Deny /
        # "Run until task done" buttons; only an explicit YES reaches the orchestrator.
        # A NO (or an unavailable approval channel) returns a message telling the AI that the
        # user decided not to run this code - nothing is executed, ever.
        # ============================================================
        if tool_name == "python_exec":
            if await self._ask_user_for_python_exec(tool_args, websocket, conversation_id):
                pass  # approved (or auto-run active for this task) -> fall through and execute
            else:
                logger.warning("[EXEC-APPROVAL] python_exec NOT executed - user declined (or approval unavailable)")
                deny_msg = ("USER DECISION: The user on this machine chose NOT to run this python_exec code. "
                             "The code was NOT executed and must not be re-submitted or worked around in any form. "
                             "Continue the task without executing that code - e.g. write it out as a file for the user "
                             "to review/run manually, explain what it would do, or proceed with an alternative approach "
                             "that does not require running it.")
                return deny_msg, ""

        # ============================================================
        # USER APPROVAL GATE for URL-bearing browser tools (2026-07-15).
        # Pre-checks the SSRF guard BEFORE execution: when a blocked target is
        # CONSENTABLE (file:// inside working root / loopback http(s)) it asks the
        # local user; on Allow it registers the one-shot grant so the tool's own
        # ensure_url_not_ssrff() passes for this exact URL. Hard-blocked targets and
        # a declined question fail closed with an explanatory message (no execution).
        # ============================================================
        if tool_name in URL_CONSENT_TOOLS:
            url_arg = (tool_args or {}).get("url") if isinstance(tool_args, dict) else None
            if isinstance(url_arg, str) and url_arg.strip():
                if await self._maybe_ask_url_consent(url_arg, websocket, conversation_id):
                    pass  # allowed (grant registered / auto-consent active) -> fall through
                else:
                    logger.warning("[URL-CONSENT] %s NOT executed - blocked target declined or unconsentable", tool_name)
                    deny_msg = ("USER DECISION: This URL was refused by the local user's browser-security gate. "
                                f"The navigation to {url_arg[:200]} did NOT happen and must not be retried with the same URL. "
                                "If it is a local file inside your working folder, tell the user to click Allow on the "
                                "approval card (or ask them again in a new task). Otherwise use an alternative approach: "
                                "serve the file over http://localhost, read it as text with the file tools, or explain what you intended.")
                    return deny_msg, ""

        # Send tool start notification (skip if silent mode)
        if not silent:
            logger.debug(f"[AGENTIC.DEBUG] ToolExecutor.execute() - Sending tool_start notification for '{tool_name}'")
            await websocket.send_text(json.dumps({
                "type": "tool_start",
                "tool": tool_name,
                "input": str(tool_args)
            }))

        # Execute the tool -- handle both sync and async orchestrators
        if self._is_async_orchestrator:
            # RemoteToolOrchestrator -- execute_tool is a coroutine (may fetch from SERVER)
            tool_result = await self.orchestrator.execute_tool(
                tool_name,
                tool_args,
                conversation_history=conversation_history,
                api_key=self.api_key
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
                api_key=self.api_key
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
                "output": result_str[:AGENTIC_TOOL_OUTPUT_MAX_CHARS]
            }))
            return result_str, ""

        # Send tool end notification (skip if silent mode)
        if not silent:
            logger.debug(f"[AGENTIC.DEBUG] ToolExecutor.execute() - Sending tool_end notification for '{tool_name}'")
            await websocket.send_text(json.dumps({
                "type": "tool_end",
                "tool": tool_name,
                "output": result_str[:AGENTIC_TOOL_OUTPUT_MAX_CHARS]
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
        return result_str, agent_lessons
    # ------------------------------------------------------------------
    # python_exec user approval (2026-09-24) - see app/chat_bus/exec_approval.py
    # ------------------------------------------------------------------

    async def _ask_user_for_python_exec(self, tool_args: Dict, websocket: Any,
                                        conversation_id: str = None) -> bool:
        """Ask the local user whether this python_exec code may run on THIS machine.

        Returns True only when execution is allowed (explicit 'allow' or an active
        "run until task done" grant for this agentic run). Everything else - deny,
        missing channel/state, undeliverable frame - fails closed and returns False so
        the code NEVER runs without user consent.

        There is NO timeout on the question: wait_for_decision() blocks until the user
        answers (or the turn is stopped), so the user has all the time in the world.

        The approval state lives on the chat's ChatChannel (fresh per turn), so a
        "run until task done" grant lasts exactly one agentic run: the next loop run
        starts with a new ExecApprovalState and asks again.
        """
        from app.chat_bus import get_chat_bus, build_approval_frame

        code = (tool_args or {}).get("code", "") if isinstance(tool_args, dict) else ""
        if not isinstance(code, str) or not code.strip():
            # Empty/invalid payload: let python_exec() itself answer with its own message.
            return True

        bus = get_chat_bus()
        state = None
        if bus is not None and conversation_id:
            channel = bus.channels.get(conversation_id)
            if channel is not None:
                state = getattr(channel, "exec_approval", None)
        # Fail-closed: no live approval state (non-bus context / stale channel) -> refuse.
        if state is None or not hasattr(state, "wait_for_decision"):
            logger.warning("[EXEC-APPROVAL] no approval state for conv=%s - failing closed (not executed)", conversation_id)
            return False

        if getattr(state, "auto_run_all", False):
            logger.info("[EXEC-APPROVAL] auto-run active for this task - python_exec allowed without asking")
            return True

        request_id = state.new_request_id()
        try:
            await websocket.send_text(json.dumps(build_approval_frame(conversation_id, request_id, code)))
        except Exception as e:
            logger.warning(f"[EXEC-APPROVAL] approval frame not delivered ({e}) - failing closed")
            return False

        decision = await state.wait_for_decision(request_id)

        if decision == "run_all":
            # Grant is now active for the REST OF THIS RUN (state.auto_run_all=True).
            try:
                await websocket.send_text(json.dumps({
                    "type": "system",
                    "content": "Auto-run enabled: python_exec will run without asking until this task ends.",
                }))
            except Exception:
                pass  # dead socket - the grant still holds in state; card shows it client-side
            return True

        if decision == "allow":
            return True

        # 'deny' -> fail-closed (the only remaining decision).
        logger.info(f"[EXEC-APPROVAL] python_exec blocked by user decision: {decision}")
        return False

    # ------------------------------------------------------------------
    # URL consent gate for browser tools (2026-07-15) - see app/chat_bus/exec_approval.py
    # ------------------------------------------------------------------

    async def _maybe_ask_url_consent(self, url: str, websocket: Any,
                                     conversation_id: str = None) -> bool:
        """Decide whether a URL-bearing browser tool may proceed to *url*.

        Returns True when the SSRF guard already accepts the URL (normal public web -
        no user interaction at all), or after an explicit user Allow for a CONSENTABLE
        local target (file:// inside working root, loopback http(s)) -- in that case a
        one-shot exact-match grant is registered so the tool's own ensure_url_not_ssrff()
        passes. Returns False (fail-closed) when the target is hard-blocked by policy or
        the user declined / could not be asked. Never raises.

        "Run until task done" on a URL consent card behaves like python_exec: every later
        consentable local target of THIS agentic run passes without asking; dangerous
        (non-consentable) targets are still hard-blocked even under auto-consent.
        """
        try:
            from tools.ssrf_guard import validate_url_not_ssrff, classify_blocked_url, grant_url
        except Exception as e:  # guard unavailable -> let the tool's own check answer (it fails closed there)
            logger.warning(f"[URL-CONSENT] ssrf_guard unavailable ({e!r}) - skipping pre-gate")
            return True

        safe, _reason = validate_url_not_ssrff(url)
        if safe:
            return True  # public web target -- normal path, no question asked

        consentable, why = classify_blocked_url(url)
        if not consentable:
            logger.info(f"[URL-CONSENT] hard block (not consentable): {url} - {why}")
            return False

        from app.chat_bus import get_chat_bus, build_approval_frame
        bus = get_chat_bus()
        state = None
        if bus is not None and conversation_id:
            channel = bus.channels.get(conversation_id)
            if channel is not None:
                state = getattr(channel, "exec_approval", None)
        # Fail-closed: no live approval state (non-bus context / stale channel) -> refuse.
        if state is None or not hasattr(state, "wait_for_decision"):
            logger.warning("[URL-CONSENT] no approval state for conv=%s - failing closed (not navigated)", conversation_id)
            return False

        if getattr(state, "auto_run_all", False):
            # Auto-consent active for this run: consentable local targets pass without asking.
            grant_url(url)
            logger.info("[URL-CONSENT] auto-consent active - allowed %s without asking", url[:120])
            return True

        request_id = state.new_request_id()
        try:
            await websocket.send_text(json.dumps(build_approval_frame(
                conversation_id, request_id, url,
                title="Open this local target in the browser?",
                description=("This URL points to THIS machine (a file inside your working "
                             "folder or a localhost address). The SSRF guard blocked it by default; "
                             "Allow opens exactly this one URL."),
            )))
        except Exception as e:
            logger.warning(f"[URL-CONSENT] approval frame not delivered ({e}) - failing closed")
            return False

        decision = await state.wait_for_decision(request_id)

        if decision == "run_all":
            grant_url(url)  # this one now, plus auto-consent for the rest of the run
            try:
                await websocket.send_text(json.dumps({
                    "type": "system",
                    "content": "Auto-open enabled: local file/localhost targets will open without asking until this task ends.",
                }))
            except Exception:
                pass  # dead socket - the grant still holds in state; card shows it client-side
            return True

        if decision == "allow":
            grant_url(url)
            logger.info(f"[URL-CONSENT] user allowed local target: {url[:120]}")
            return True

        logger.info(f"[URL-CONSENT] local target blocked by user decision: {decision} ({url[:120]})")
        return False
