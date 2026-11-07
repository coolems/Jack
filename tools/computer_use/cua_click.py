"""cua_click - press and release a mouse button on an element or a coordinate.

Ported semantics, three mutually exclusive target modes:
  * element: snapshotId + elementIndex (UIA row) - clicked at the element's own
    clickable point, or its centre when UIA reports none
  * absolute: x,y in window-local pixels from the LATEST cua_screenshot of that window;
    rejected with WINDOW_CHANGED if the window moved/resized since that capture
  * relative: [rx, ry] thousandths of the window (0..999), scaled and clamped inside it

A press is ALWAYS followed by a release, even if the hold raised, so a button can never
stay stuck. The result says dispatched=true, verified=false: delivered input is not
proof of an outcome - re-observe before concluding.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_click",
        "description": "Click a UIA element (snapshotId+elementIndex) or a window point (window px from the latest cua_screenshot, or relative 0..999). Returns dispatched=true, verified=false - re-observe to confirm. hold_ms clamped; release always happens.",
        "parameters": {
            "type": "object",
            "properties": {
                "window_id": {"type": "string", "description": "windowId for coordinate targets."},
                "snapshot_id": {"type": "string", "description": "snapshotId for an element target."},
                "element_index": {"type": "integer", "description": "Element index in that snapshotId."},
                "x": {"type": "integer", "description": "X (window px)."},
                "y": {"type": "integer", "description": "Y (window px)."},
                "relative": {"type": "array", "items": {"type": "integer"}, "description": "[rx, ry] thousandths 0..999."},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button."},
                "clicks": {"type": "integer", "description": "1 or 2."},
                "hold_ms": {"type": "integer", "description": "Hold ms per press (max 5000)."}
            },
            "required": []
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_session_locked, cua_window_rect
from ._desktop_input import cua_move_cursor, cua_press
from ._desktop_state import (action_error, get_window, resolve_element_target,
                             resolve_window_target, target_consent_key)
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Click")

MAX_HOLD_MS = 5000


def _target_point(window_id, snapshot_id, element_index, x, y, relative):
    """Resolve a click target to absolute screen pixels. Returns (point, error)."""
    if snapshot_id is not None:
        return resolve_element_target(snapshot_id, element_index)
    window = get_window(window_id or "")
    if window is None:
        return None, (f"WINDOW_REQUIRED: window {window_id!r} is not registered. "
                      "Call cua_list_windows first.")
    return resolve_window_target(window_id, cua_window_rect(window["hwnd"]),
                                 x=x, y=y, relative=relative)


def cua_click(window_id: str = None, snapshot_id: str = None, element_index: int = None,
              x: int = None, y: int = None, relative: list = None,
              button: str = "left", clicks: int = 1, hold_ms: int = 0) -> dict:
    """Click an element or a window coordinate."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    if cua_session_locked():
        return {"status": "error",
                "error": ("SESSION_LOCKED: the desktop is locked. Ask the user to unlock it - input "
                          "sent now reaches the lock screen, not the app.")}
    if button not in ("left", "right", "middle"):
        return action_error(f"INVALID_BUTTON: {button!r}. Use left, right or middle.")
    clicks = max(1, min(int(clicks or 1), 2))
    hold = max(0, min(int(hold_ms or 0), MAX_HOLD_MS))

    conv_id = current_conversation_id()
    # Resolve the target's identity FIRST, so a stale snapshotId or an unregistered windowId
    # reports its ACTUAL reason (SNAPSHOT_INVALIDATED / WINDOW_REQUIRED) instead of the
    # generic 'not granted'. Still fail-closed: an unidentifiable target has no grant key.
    key, err = target_consent_key(snapshot_id, window_id)
    if err:
        return action_error(err)
    if not is_granted(key, conv_id):
        return action_error("DESKTOP_CONSENT_REQUIRED: this app is not granted for this session.")

    point, err = _target_point(window_id, snapshot_id, element_index, x, y, relative)
    if err:
        return action_error(err)

    target, actual = cua_move_cursor(point[0], point[1])
    if actual is None or (abs(actual[0] - target[0]) > 12 or abs(actual[1] - target[1]) > 12):
        return {"status": "error",
                "error": (f"CURSOR_NOT_MOVED: asked for {target}, cursor reads {actual}. "
                          "Something else owns the pointer - stop and tell the user."),
                "dispatched": False, "verified": False}

    pressed = True
    for _ in range(clicks):
        pressed = pressed and cua_press(button, hold)

    logger.info("[CUA] click %s at %s clicks=%d hold=%d dispatched=%s", button, target, clicks, hold, pressed)
    return {"status": "ok" if pressed else "error",
            "dispatched": bool(pressed), "verified": False,
            "target": {"x": target[0], "y": target[1]},
            "cursor_after": {"x": actual[0], "y": actual[1]},
            "button": button, "clicks": clicks, "hold_ms": hold,
            "error": None if pressed else "SEND_INPUT_REFUSED: SendInput queued no events.",
            "next_step": "Re-observe with cua_get_state or cua_screenshot - a delivered click is not proof."}
