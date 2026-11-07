"""cua_get_state - observe an app's control tree through UI Automation.

Ported of getState: the observation returns a snapshotId plus element indices. Indices
belong to THIS snapshot only - re-observing the same window invalidates the previous
one (see _desktop_state.register_snapshot), so a stale index is refused instead of
acting on a different control.

Rows are capped and reported honestly (shown/truncated): a partial tree is never
presented as the whole window.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_get_state",
        "description": "Observe a window's UIA control tree: snapshotId + indexed elements (name, control_type, rect, actions, offscreen). Indices are valid only for THIS snapshotId - re-observing invalidates the old ones. Read-only; sends no input.",
        "parameters": {
            "type": "object",
            "properties": {
                "app_handle": {"type": "string", "description": "app_handle from cua_get_app."},
                "window_id": {"type": "string", "description": "windowId from cua_get_app/cua_list_windows."},
                "include_text": {"type": "boolean", "description": "Also read each element's text (Text pattern). Slower and larger."}
            },
            "required": ["app_handle", "window_id"]
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_window_rect
from ._desktop_state import (app_consent_key, get_app, get_window, new_handle_token,
                             rect_is_minimized, register_snapshot)
from ._desktop_uia import uia_available, uia_snapshot, uia_read_text, uia_resolve
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.GetState")

#: Rows returned to the model. The walk itself is larger; the rest is reported as truncated.
MAX_SHOWN = 200


def cua_get_state(app_handle: str, window_id: str, include_text: bool = False) -> dict:
    """Snapshot one window's control tree."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    if not uia_available():
        return {"status": "error",
                "error": "UIA_UNAVAILABLE: UI Automation cannot be created in this process."}

    record = get_app(app_handle)
    if record is None:
        return {"status": "error",
                "error": f"APP_REQUIRED: app_handle {app_handle} is unknown or was revoked."}
    if not is_granted(app_consent_key(app_handle), current_conversation_id()):
        return {"status": "error", "error": "DESKTOP_CONSENT_REQUIRED: access to this app is not granted."}

    window = get_window(window_id)
    if window is None:
        return {"status": "error",
                "error": (f"WINDOW_REQUIRED: window {window_id} is not registered. "
                          "Call cua_list_windows first.")}

    rect = cua_window_rect(window["hwnd"])
    if rect is None:
        return {"status": "error",
                "error": f"WINDOW_CHANGED: window {window_id} has no readable rectangle (closed)."}
    if rect_is_minimized(rect):
        return {"status": "error",
                "error": (f"WINDOW_MINIMIZED: window {window_id} is off-screen. "
                          "Use cua_show_window, then observe again.")}

    elements, truncated, err = uia_snapshot(window["hwnd"])
    if err:
        return {"status": "error", "error": err}

    if include_text:
        for row in elements:
            try:
                element = uia_resolve(window["hwnd"], row["path"])
                if element is not None:
                    row["text"] = uia_read_text(element)
            except Exception as e:
                logger.debug("text read skipped: %s", e)

    snapshot_id = new_handle_token()
    register_snapshot(snapshot_id, app_handle, window_id, elements, rect, truncated, window["hwnd"])

    shown = elements[:MAX_SHOWN]
    return {"status": "ok", "snapshotId": snapshot_id, "windowId": window_id,
            "app": record["app_name"], "rect": rect, "element_count": len(elements),
            "elements": shown, "shown": len(shown), "truncated": truncated or len(elements) > MAX_SHOWN,
            "next_step": ("Act with a snapshotId + elementIndex, or cua_screenshot for pixels. "
                          "Indices are valid only for this snapshotId.")}
