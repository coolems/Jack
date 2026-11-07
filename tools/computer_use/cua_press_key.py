"""cua_press_key - one key (optionally with modifiers) into the granted app.

Same focus guard as cua_type_text: the keystroke only goes out when UIA's focused
element belongs to the granted app's pid, otherwise FOCUS_MISMATCH refuses it.

Chord syntax mirrors their rule: modifiers join with '+', so 'cmd++' is Command-plus and
a trailing 'cmd+' is rejected as an incomplete chord. Key names come from the fixed tables
in _desktop_input (named keys plus single characters) - an arbitrary key code is never
honoured. Modifiers are always released, even if the key event fails.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_press_key",
        "description": "Press one named key in the granted app's focused field, with optional modifiers joined by '+' (e.g. 'ctrl+c', 'cmd++', 'enter'). Refused if focus is on another app. dispatched=true, verified=false.",
        "parameters": {
            "type": "object",
            "properties": {
                "app_handle": {"type": "string", "description": "app_handle from cua_get_app."},
                "key": {"type": "string", "description": "Key name or chord, e.g. 'enter', 'ctrl+c', 'f5', 'kp0'."}
            },
            "required": ["app_handle", "key"]
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_session_locked, cua_focus_allowed
from ._desktop_input import VK_CODES, cua_press_chord, cua_vk_for
from ._desktop_state import action_error, app_consent_key, get_app
from ._desktop_uia import uia_focused_pid
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.KeyPress")


MODIFIER_KEYS = ("ctrl", "control", "alt", "shift", "cmd", "win", "menu")


def _parse_chord(key: str):
    """Split a chord into (key_name, modifiers). Returns (None, error) when malformed."""
    raw = str(key).strip()
    parts = [p.strip().lower() for p in raw.split("+") if p.strip()]
    if not parts:
        return None, "EMPTY_KEY: give a key name like 'enter' or a chord like 'ctrl+c'."

    # A literal '+' key: their chord syntax writes Command-plus as 'cmd++'. split('+') drops
    # both the separator and the bare key, so the trailing PAIR is what proves the key was '+'.
    # 'cmd+' (one trailing separator) stays an incomplete chord - never guessed.
    if raw.endswith("++"):
        if any(p not in MODIFIER_KEYS for p in parts):
            return None, f"UNKNOWN_MODIFIER in {key!r}. Use ctrl, alt, shift or cmd."
        return "+", list(parts)

    if len(parts) == 1:
        # 'cmd+' style trailing separator: parts == ['cmd'] is a modifier with no key.
        if parts[0] in MODIFIER_KEYS:
            return None, f"INCOMPLETE_CHORD: {key!r} names only a modifier. Add a key, e.g. 'cmd+c'."
        return parts[0], []
    modifiers = [p for p in parts[:-1] if p in MODIFIER_KEYS]
    final = parts[-1]
    if final in MODIFIER_KEYS:
        return None, f"INCOMPLETE_CHORD: {key!r} ends with a modifier. Put the real key last."
    if not modifiers and len(parts) > 1:
        return None, f"UNKNOWN_MODIFIER in {key!r}. Use ctrl, alt, shift or cmd."
    return final, modifiers


def cua_press_key(app_handle: str, key: str) -> dict:
    """Press one key (or chord) in the granted app's focused control."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    if cua_session_locked():
        return {"status": "error", "error": "SESSION_LOCKED: the desktop is locked - ask the user to unlock it."}
    record = get_app(app_handle)
    if record is None:
        return action_error(f"APP_REQUIRED: app_handle {app_handle} is unknown or was revoked.")
    if not is_granted(app_consent_key(app_handle), current_conversation_id()):
        return action_error("DESKTOP_CONSENT_REQUIRED: access to this app is not granted.")

    name, modifiers = _parse_chord(key)
    if name is None:
        return action_error(modifiers)
    if cua_vk_for(name) is None:
        known = ", ".join(sorted(set(VK_CODES))[:24])
        return action_error(f"UNKNOWN_KEY: {name!r}. Known keys include: {known}, f1-f12, kp0-kp9, "
                            "and single characters (letters, digits, punctuation).")

    focused_pid = uia_focused_pid()
    if focused_pid is None:
        return action_error("FOCUS_UNKNOWN: UIA cannot read the focused element, so the key could land "
                            "anywhere. Click a field in the target window first (cua_click).")
    if not cua_focus_allowed(record["pid"], focused_pid, record.get("exe_path", "")):
        return action_error(f"FOCUS_MISMATCH: keyboard focus is on pid {focused_pid}, which does not "
                            f"run the granted app ({record['exe_path'] or record['app_name']}, "
                            f"pid {record['pid']}). Do not send keys to an app the user did not allow.")

    ok = cua_press_chord(name, modifiers)
    logger.info("[CUA] key %s (mods=%s) -> pid=%s dispatched=%s", name, modifiers, record["pid"], ok)
    return {"status": "ok" if ok else "error", "dispatched": bool(ok), "verified": False,
            "key": name, "modifiers": modifiers, "focused_pid": focused_pid,
            "error": None if ok else "SEND_INPUT_REFUSED: SendInput queued no events.",
            "next_step": "Re-observe - a delivered key is not proof the app acted on it."}
