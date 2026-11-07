"""cua_scroll - wheel-scroll at a point inside one window.

The pointer is positioned first (Windows delivers the wheel to whatever is under the
cursor), so the same coordinate rules apply: WINDOW_CHANGED on a moved/resized window,
WINDOW_MINIMIZED refused, relative values clamped inside the window. amount is capped so
one call can never scroll thousands of lines.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_scroll",
        "description": "Wheel-scroll inside one window at a point (window px, relative 0..999, or a snapshot element). amount = wheel clicks 1-20. dispatched=true, verified=false.",
        "parameters": {
            "type": "object",
            "properties": {
                "window_id": {"type": "string", "description": "windowId to scroll in."},
                "direction": {"type": "string", "enum": ["up", "down"], "description": "Wheel direction."},
                "amount": {"type": "integer", "description": "Wheel clicks 1-20 (default 3)."},
                "x": {"type": "integer", "description": "X (window px); default window centre."},
                "y": {"type": "integer", "description": "Y (window px); default window centre."},
                "relative": {"type": "array", "items": {"type": "integer"}, "description": "[rx, ry] thousandths 0..999."},
                "snapshot_id": {"type": "string", "description": "Optional element snapshotId."},
                "element_index": {"type": "integer", "description": "Element index."}
            },
            "required": ["window_id"]
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_session_locked, cua_window_rect
from ._desktop_input import cua_move_cursor, cua_wheel, cua_wheel_delta
from ._desktop_state import (action_error, get_window, resolve_element_target,
                             resolve_window_target, target_consent_key)
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Scroll")


def cua_scroll(window_id: str, direction: str = "down", amount: int = 3,
               x: int = None, y: int = None, relative: list = None,
               snapshot_id: str = None, element_index: int = None) -> dict:
    """Wheel-scroll at a point inside one window."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    if cua_session_locked():
        return {"status": "error", "error": "SESSION_LOCKED: the desktop is locked - ask the user to unlock it."}
    if direction not in ("up", "down"):
        return action_error(f"INVALID_DIRECTION: {direction!r}. Use up or down.")
    clicks = max(1, min(int(amount or 3), 20))

    conv_id = current_conversation_id()
    key, err = target_consent_key(snapshot_id, window_id)
    if err:
        return action_error(err)
    if not is_granted(key, conv_id):
        return action_error("DESKTOP_CONSENT_REQUIRED: this app is not granted for this session.")

    if snapshot_id is not None:
        point, err = resolve_element_target(snapshot_id, element_index)
    else:
        window = get_window(window_id or "")
        if window is None:
            return action_error(f"WINDOW_REQUIRED: window {window_id!r} is not registered. Call cua_list_windows first.")
        rect = cua_window_rect(window["hwnd"])
        if rect is None:
            return action_error(f"WINDOW_CHANGED: window {window_id} has no readable rectangle.")
        if x is None and y is None and relative is None:
            # No point given: scroll over the window centre rather than wherever the
            # cursor happens to be - that keeps the action inside the granted window.
            point, err = resolve_window_target(window_id, rect,
                                               x=rect["width"] // 2, y=rect["height"] // 2)
        else:
            point, err = resolve_window_target(window_id, rect, x=x, y=y, relative=relative)
    if err:
        return action_error(err)

    target, actual = cua_move_cursor(point[0], point[1])
    if actual is None or (abs(actual[0] - target[0]) > 12 or abs(actual[1] - target[1]) > 12):
        return {"status": "error",
                "error": f"CURSOR_NOT_MOVED: asked for {target}, cursor reads {actual}.",
                "dispatched": False, "verified": False}

    delta = cua_wheel(direction, clicks)
    logger.info("[CUA] scroll %s x%d at %s events=%d", direction, clicks, target, delta)
    return {"status": "ok" if delta else "error", "dispatched": bool(delta), "verified": False,
            "target": {"x": target[0], "y": target[1]}, "direction": direction,
            "clicks": clicks, "events_sent": delta, "wheel_delta": cua_wheel_delta(direction, clicks),
            "error": None if delta else "SEND_INPUT_REFUSED: SendInput queued no wheel events.",
            "next_step": ("Re-observe with cua_get_state - a delivered wheel is not proof the content "
                          "moved. A scrollable element reports its position as scroll_percent.")}
