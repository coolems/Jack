"""cua_type_text - send text to the app that currently holds keyboard focus.

Windows delivers keystrokes to the focused control of the foreground window, so this
tool VERIFIES focus before typing: UIA's focused element must belong to the granted app
(or a process running the same executable - Chrome's web content lives in child processes
of chrome.exe). If focus sits on a different application the call is refused (FOCUS_MISMATCH)
instead of typing into something the user never approved.

Characters are sent as virtual-key events (Shift held for shifted ones, always released).
Verified live: Chrome's web form fields ignore KEYF_UNICODE events but accept VK events, so
unicode events are only used for characters the VK table cannot express.
dispatched/verified=false: delivered characters are not proof of insertion - re-observe the field.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_type_text",
        "description": "Type text into the granted app's focused field (refused if focus is on another app). VK keystrokes, max 2000 chars. dispatched=true, verified=false.",
        "parameters": {
            "type": "object",
            "properties": {
                "app_handle": {"type": "string", "description": "app_handle from cua_get_app."},
                "text": {"type": "string", "description": "Text to type."}
            },
            "required": ["app_handle", "text"]
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_session_locked, cua_focus_allowed
from ._desktop_input import cua_type_keys
from ._desktop_state import action_error, app_consent_key, get_app
from ._desktop_uia import uia_focused_pid
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.TypeText")

MAX_TEXT_CHARS = 2000


def cua_type_text(app_handle: str, text: str) -> dict:
    """Type text into the granted app's focused control."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    if cua_session_locked():
        return {"status": "error", "error": "SESSION_LOCKED: the desktop is locked - ask the user to unlock it."}
    record = get_app(app_handle)
    if record is None:
        return action_error(f"APP_REQUIRED: app_handle {app_handle} is unknown or was revoked.")
    if not is_granted(app_consent_key(app_handle), current_conversation_id()):
        return action_error("DESKTOP_CONSENT_REQUIRED: access to this app is not granted.")
    if not isinstance(text, str) or not text:
        return action_error("EMPTY_TEXT: nothing to type.")
    if len(text) > MAX_TEXT_CHARS:
        return action_error(f"TEXT_TOO_LONG: {len(text)} chars (limit {MAX_TEXT_CHARS}). Split it.")

    focused_pid = uia_focused_pid()
    if focused_pid is None:
        return action_error("FOCUS_UNKNOWN: UIA cannot read the focused element, so typing could land "
                            "anywhere. Click a field in the target window first (cua_click).")
    if not cua_focus_allowed(record["pid"], focused_pid, record.get("exe_path", "")):
        return action_error(f"FOCUS_MISMATCH: keyboard focus is on pid {focused_pid}, which does not "
                            f"run the granted app ({record['exe_path'] or record['app_name']}, "
                            f"pid {record['pid']}). Click a field in the target window first - do not "
                            "type into an app the user did not allow.")

    sent = cua_type_keys(text[:MAX_TEXT_CHARS])
    logger.info("[CUA] typed %d chars into pid=%s (events=%d)", len(text), record["pid"], sent)
    return {"status": "ok" if sent else "error", "dispatched": bool(sent), "verified": False,
            "characters": len(text), "events_sent": sent,
            "focused_pid": focused_pid,
            "error": None if sent else "SEND_INPUT_REFUSED: SendInput queued no events.",
            "next_step": "Re-observe the field (cua_get_state with include_text) - delivered keys are not proof."}
