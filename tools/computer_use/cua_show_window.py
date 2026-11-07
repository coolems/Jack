"""cua_show_window - unhide/unminimize a window WITHOUT activating it.

Their show semantics: activation steals focus from the user, so this only makes the
window addressable (a minimized window has no pixels and no usable UIA tree) and
reports whether the foreground changed. cua_activate_window is the separate, explicit
call that takes focus - it is deliberately not folded in here.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_show_window",
        "description": "Unhide/unminimize a window without activating it (no focus steal), so it has pixels and a control tree again. Reports foregroundChanged. Not proof - re-observe.",
        "parameters": {
            "type": "object",
            "properties": {
                "window_id": {"type": "string", "description": "windowId from cua_get_app/cua_list_windows."},
                "restore": {"type": "boolean", "description": "Also restore a minimized window to normal size."}
            },
            "required": ["window_id"]
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_foreground_hwnd, cua_show_window as win_show, cua_window_rect
from ._desktop_state import action_error, get_window, rect_is_minimized, window_consent_key
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.ShowWindow")


def cua_show_window(window_id: str, restore: bool = False) -> dict:
    """Make one window visible without bringing it to the foreground."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    window = get_window(window_id)
    if window is None:
        return action_error(f"WINDOW_REQUIRED: window {window_id} is not registered. "
                            "Call cua_get_app or cua_list_windows first.")
    if not is_granted(window_consent_key(window_id), current_conversation_id()):
        return action_error("DESKTOP_CONSENT_REQUIRED: access to this app is not granted.")

    before_foreground = cua_foreground_hwnd()
    landed = win_show(window["hwnd"], restore=restore)
    rect = cua_window_rect(window["hwnd"])
    logger.info("[CUA] show window %s restore=%s landed=%s", window_id, restore, landed)
    return {"status": "ok" if landed else "error", "dispatched": bool(landed), "verified": False,
            "windowId": window_id, "restore": bool(restore),
            "rect": rect, "on_screen": rect is not None and not rect_is_minimized(rect),
            "foregroundChanged": cua_foreground_hwnd() != before_foreground,
            "error": None if landed else "SHOW_REFUSED: ShowWindow returned false.",
            "next_step": "Re-run cua_list_windows, then cua_get_state or cua_screenshot."}
