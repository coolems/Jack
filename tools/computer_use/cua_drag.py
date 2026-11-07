"""cua_drag - hold a mouse button while moving the pointer inside one window.

Same three target modes as cua_click. Both endpoints are validated against the window's
CURRENT rectangle, so a window that moved since the screenshot is refused (WINDOW_CHANGED)
instead of dragging across the desktop. The release always happens in a finally block - a
drag that fails mid-way cannot leave the button pressed.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_drag",
        "description": "Drag from one point to another inside one window (window px, relative 0..999, or a snapshot element as the start). Button always released even on failure. dispatched=true, verified=false.",
        "parameters": {
            "type": "object",
            "properties": {
                "window_id": {"type": "string", "description": "windowId owning both endpoints."},
                "from_x": {"type": "integer", "description": "Start X (window px)."},
                "from_y": {"type": "integer", "description": "Start Y (window px)."},
                "to_x": {"type": "integer", "description": "End X (window px)."},
                "to_y": {"type": "integer", "description": "End Y (window px)."},
                "from_relative": {"type": "array", "items": {"type": "integer"}, "description": "Start thousandths."},
                "to_relative": {"type": "array", "items": {"type": "integer"}, "description": "End thousandths."},
                "snapshot_id": {"type": "string", "description": "Optional start element snapshotId."},
                "element_index": {"type": "integer", "description": "Start element index."},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Button held."},
                "steps": {"type": "integer", "description": "Pointer steps 2-40 (default 20)."}
            },
            "required": ["window_id"]
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_session_locked, cua_window_rect
from ._desktop_input import cua_drag as win_drag
from ._desktop_state import (action_error, get_window, resolve_element_target,
                             resolve_window_target, target_consent_key)
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Drag")


def cua_drag(window_id: str, from_x: int = None, from_y: int = None,
             to_x: int = None, to_y: int = None, from_relative: list = None,
             to_relative: list = None, snapshot_id: str = None, element_index: int = None,
             button: str = "left", steps: int = 20) -> dict:
    """Drag inside one window from one point to another."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    if cua_session_locked():
        return {"status": "error", "error": "SESSION_LOCKED: the desktop is locked - ask the user to unlock it."}
    if button not in ("left", "right", "middle"):
        return action_error(f"INVALID_BUTTON: {button!r}. Use left, right or middle.")

    conv_id = current_conversation_id()
    key, err = target_consent_key(snapshot_id, window_id)
    if err:
        return action_error(err)
    if not is_granted(key, conv_id):
        return action_error("DESKTOP_CONSENT_REQUIRED: this app is not granted for this session.")

    window = get_window(window_id or "")
    if window is None:
        return action_error(f"WINDOW_REQUIRED: window {window_id!r} is not registered. Call cua_list_windows first.")
    rect = cua_window_rect(window["hwnd"])

    if snapshot_id is not None:
        start, err = resolve_element_target(snapshot_id, element_index)
    else:
        start, err = resolve_window_target(window_id, rect, x=from_x, y=from_y, relative=from_relative)
    if err:
        return action_error(err)

    end, err = resolve_window_target(window_id, rect, x=to_x, y=to_y, relative=to_relative)
    if err:
        return action_error(err)

    done = win_drag(start, end, button=button, steps=steps)
    logger.info("[CUA] drag %s %s -> %s dispatched=%s", button, start, end, done)
    return {"status": "ok" if done else "error", "dispatched": bool(done), "verified": False,
            "from": {"x": start[0], "y": start[1]}, "to": {"x": end[0], "y": end[1]},
            "button": button, "steps": max(2, min(int(steps or 20), 40)),
            "error": None if done else "SEND_INPUT_REFUSED: SendInput queued no events.",
            "next_step": "Re-observe with cua_get_state or cua_screenshot - a delivered drag is not proof."}
