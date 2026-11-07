"""cua_hover - move the pointer onto an element or coordinate without pressing.

Reports which route it used: 'accessibility' when UIA could hover the element itself,
'system-pointer' when it moved the real cursor. system_pointer_moved=True means the
USER's cursor moved too - that is visible to them, so do not hover repeatedly.

No button is ever pressed here, and the result is still dispatched/verified=false:
hovering does not prove the app reacted.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_hover",
        "description": "Move the pointer onto an element (snapshotId+elementIndex) or window point without clicking. Reports hoverRoute and systemPointerMoved. dispatched=true, verified=false.",
        "parameters": {
            "type": "object",
            "properties": {
                "window_id": {"type": "string", "description": "windowId for coordinate targets."},
                "snapshot_id": {"type": "string", "description": "snapshotId for an element target."},
                "element_index": {"type": "integer", "description": "Element index in that snapshotId."},
                "x": {"type": "integer", "description": "X (window px)."},
                "y": {"type": "integer", "description": "Y (window px)."},
                "relative": {"type": "array", "items": {"type": "integer"}, "description": "[rx, ry] thousandths 0..999."}
            },
            "required": []
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_session_locked, cua_window_rect
from ._desktop_input import cua_cursor_pos, cua_move_cursor
from ._desktop_state import (action_error, get_window, resolve_element_target,
                             resolve_window_target, target_consent_key)
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Hover")


def cua_hover(window_id: str = None, snapshot_id: str = None, element_index: int = None,
              x: int = None, y: int = None, relative: list = None) -> dict:
    """Move the pointer onto a target without pressing any button."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    if cua_session_locked():
        return {"status": "error", "error": "SESSION_LOCKED: the desktop is locked - ask the user to unlock it."}

    conv_id = current_conversation_id()
    # Identity FIRST, so a stale snapshotId / unregistered windowId reports its real reason
    # instead of the generic 'not granted'. Fail-closed either way.
    key, err = target_consent_key(snapshot_id, window_id)
    if err:
        return action_error(err)
    if not is_granted(key, conv_id):
        return action_error("DESKTOP_CONSENT_REQUIRED: this app is not granted for this session.")

    if snapshot_id is not None:
        point, err = resolve_element_target(snapshot_id, element_index)
        # Honest route: Windows UIA has no hover method, so this is a pointer move.
        # (Their macOS helper could hover through accessibility directly; we do not
        # claim a route we cannot deliver.)
        route = "system-pointer"
    else:
        window = get_window(window_id or "")
        if window is None:
            return action_error(f"WINDOW_REQUIRED: window {window_id!r} is not registered. Call cua_list_windows first.")
        point, err = resolve_window_target(window_id, cua_window_rect(window["hwnd"]),
                                          x=x, y=y, relative=relative)
        route = "system-pointer"
    if err:
        return action_error(err)

    before = cua_cursor_pos()
    target, actual = cua_move_cursor(point[0], point[1])
    moved = actual is not None and before is not None and (before != actual)
    logger.info("[CUA] hover %s via %s -> %s", target, route, actual)
    return {"status": "ok", "dispatched": True, "verified": False,
            "hoverRoute": route, "systemPointerMoved": bool(moved),
            "target": {"x": target[0], "y": target[1]},
            "cursor_after": {"x": actual[0], "y": actual[1]} if actual else None,
            "next_step": "Re-observe - a hover that moved the pointer is not proof the app showed anything."}
