"""cua_paste - insert text through the clipboard instead of keystrokes.

Ported from their paste: keystrokes can misfire (Return/Tab trigger a control's action
instead of inserting text), so text goes through the clipboard and one Ctrl+V chord.

The user's clipboard is PRESERVED: it is read first, the text is written, Ctrl+V is
sent, and the original content is restored afterwards. Focus is verified against the
granted app's pid, so the paste cannot land in an app the user did not allow.

Consumption is foreground-dependent in some apps, so this never retries automatically -
it reports dispatched/verified=false and tells you to inspect the target.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_paste",
        "description": "Insert text via clipboard + Ctrl+V into the granted app's focused field (the user's clipboard is restored afterwards). Refused if focus is elsewhere. dispatched=true, verified=false; never auto-retried.",
        "parameters": {
            "type": "object",
            "properties": {
                "app_handle": {"type": "string", "description": "app_handle from cua_get_app."},
                "text": {"type": "string", "description": "Text to paste."}
            },
            "required": ["app_handle", "text"]
        }
    }
}

import logging

from ._desktop_win import (cua_clipboard_get, cua_clipboard_set, desktop_available,
                           cua_session_locked, cua_focus_allowed)
from ._desktop_input import cua_press_chord
from ._desktop_state import action_error, app_consent_key, get_app
from ._desktop_uia import uia_focused_pid
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Paste")

MAX_PASTE_CHARS = 20000


def cua_paste(app_handle: str, text: str) -> dict:
    """Paste text into the granted app's focused control through the clipboard."""
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
        return action_error("EMPTY_TEXT: nothing to paste.")
    if len(text) > MAX_PASTE_CHARS:
        return action_error(f"TEXT_TOO_LONG: {len(text)} chars (limit {MAX_PASTE_CHARS}).")

    focused_pid = uia_focused_pid()
    if focused_pid is None:
        return action_error("FOCUS_UNKNOWN: UIA cannot read the focused element, so a paste could land "
                            "anywhere. Click a field in the target window first (cua_click).")
    if not cua_focus_allowed(record["pid"], focused_pid, record.get("exe_path", "")):
        return action_error(f"FOCUS_MISMATCH: keyboard focus is on pid {focused_pid}, which does not "
                            f"run the granted app ({record['exe_path'] or record['app_name']}, "
                            f"pid {record['pid']}). Do not paste into an app the user did not allow.")

    original = cua_clipboard_get()
    written = cua_clipboard_set(text)
    if not written:
        return {"status": "error", "error": "CLIPBOARD_WRITE_FAILED: the clipboard refused the text.",
                "dispatched": False, "verified": False}

    sent = cua_press_chord("v", ["ctrl"])

    # Restore the user's own clipboard content whatever happened above.
    restored = cua_clipboard_set(original) if original else True
    logger.info("[CUA] paste %d chars -> pid=%s chord=%s restored=%s", len(text), record["pid"], sent, restored)
    return {"status": "ok" if sent else "error", "dispatched": bool(sent), "verified": False,
            "characters": len(text), "focused_pid": focused_pid,
            "clipboard_restored": bool(restored),
            "error": None if sent else "SEND_INPUT_REFUSED: SendInput queued no Ctrl+V events.",
            "next_step": ("Inspect the target (cua_get_state with include_text). Consumption is "
                          "foreground-dependent - do NOT retry automatically.")}
