"""cua_list_windows - re-read an app's windows and refresh their rectangles.

Ported from listWindows. Rectangles are refreshed from the live window so a stale
windowId is caught here rather than at click time: a window that moved or resized is
reported as changed, and its stored rect is updated so later absolute coordinates are
validated against the NEW geometry (WINDOW_CHANGED otherwise). Nothing is activated.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_list_windows",
        "description": "List one app's windows: windowId, title, rect, on_screen. Refreshes each stored rect, so a moved/resized window is reported changed instead of letting old coordinates land elsewhere. Registers newly opened windows. Activates nothing.",
        "parameters": {
            "type": "object",
            "properties": {
                "app_handle": {"type": "string", "description": "app_handle from cua_get_app."}
            },
            "required": ["app_handle"]
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_enum_windows, cua_window_rect
from ._desktop_state import (app_consent_key, get_app, new_handle_token, register_window,
                             rect_is_minimized, windows_for_app)
from ._consent import current_conversation_id, is_granted

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.ListWindows")


def cua_list_windows(app_handle: str) -> dict:
    """Enumerate an app's windows and refresh their stored rectangles."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    record = get_app(app_handle)
    if record is None:
        return {"status": "error",
                "error": (f"APP_REQUIRED: app_handle {app_handle} is unknown or was revoked. "
                          "Call cua_get_app again.")}
    if not is_granted(app_consent_key(app_handle), current_conversation_id()):
        return {"status": "error", "error": "DESKTOP_CONSENT_REQUIRED: access to this app is not granted."}

    live_by_hwnd = {w["hwnd"]: w for w in cua_enum_windows() if w["pid"] == record["pid"]}
    out = []
    changed = 0
    known_hwnds = set()

    for window_id, stored in windows_for_app(app_handle):
        known_hwnds.add(stored["hwnd"])
        live = cua_window_rect(stored["hwnd"])
        if live is None:
            out.append({"windowId": window_id, "title": stored.get("title", ""),
                        "status": "WINDOW_CHANGED: no readable rectangle (closed or destroyed)"})
            continue
        entry = {"windowId": window_id, "title": stored.get("title", ""), "rect": live,
                 "on_screen": not rect_is_minimized(live)}
        if live != stored["rect"]:
            changed += 1
            entry["changed"] = True
            register_window(window_id, stored["hwnd"], app_handle, live)
        out.append(entry)

    for hwnd, w in live_by_hwnd.items():
        if hwnd in known_hwnds:
            continue
        window_id = new_handle_token()
        register_window(window_id, hwnd, app_handle, w["rect"])
        out.append({"windowId": window_id, "title": w["title"], "rect": w["rect"],
                    "on_screen": not rect_is_minimized(w["rect"]), "new": True})

    return {"status": "ok", "app_handle": app_handle, "pid": record["pid"],
            "windows": out, "rects_changed": changed,
            "next_step": "Take cua_screenshot of a windowId before using absolute coordinates."}
