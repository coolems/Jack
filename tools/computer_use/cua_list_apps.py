"""cua_list_apps - enumerate running apps (gated: window titles are app data).

Ported from listApps. Reads window titles/pids through EnumWindows only - nothing is
launched, activated, hidden or clicked, and no process is opened for anything but
QUERY_LIMITED_INFORMATION (identity). Titles are application data, so this call needs
the user's desktop-enumeration consent (consent kind 'desktop').

It deliberately returns NO app handles: a handle is bound by cua_get_app, which is the
call the user consents to. Listing must not hand out handles that were never approved.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_list_apps",
        "description": "List running apps as {app, pid, exe_path, window_count, on_screen} from visible window titles. Launches/activates/clicks nothing; minimized windows are reported, not hidden. Returns no handles - cua_get_app binds one (that call asks the user).",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_enum_windows, cua_process_image_path
from ._desktop_state import rect_is_minimized
from ._consent import current_conversation_id, is_granted, ENUMERATE_KEY

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.ListApps")

#: Our own UI is never a drivable target - it must not appear as an app to control.
PROTECTED_NAMES = ("coolems", "coolems_client")

MAX_APPS = 40


def cua_list_apps() -> dict:
    """Enumerate visible top-level windows grouped by owning process."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}

    conv_id = current_conversation_id()
    if not is_granted(ENUMERATE_KEY, conv_id):
        return {"status": "error",
                "error": ("DESKTOP_CONSENT_REQUIRED: listing running apps reads window titles, so "
                          "the local user must allow desktop access first (cua_get_app shows that card).")}

    by_pid: dict = {}
    for w in cua_enum_windows():
        title = w["title"]
        if any(p in title.lower() for p in PROTECTED_NAMES):
            continue
        pid = w["pid"]
        entry = by_pid.setdefault(pid, {"app": title, "pid": pid,
                                       "exe": cua_process_image_path(pid),
                                       "window_count": 0, "on_screen": False})
        entry["window_count"] += 1
        if not rect_is_minimized(w["rect"]):
            entry["on_screen"] = True

    apps = [{"app": e["app"], "pid": pid, "exe_path": e["exe"],
             "window_count": e["window_count"], "on_screen": e["on_screen"]}
            for pid, e in by_pid.items()]
    apps.sort(key=lambda a: (-a["window_count"], a["app"]))

    return {"status": "ok", "apps": apps[:MAX_APPS], "total": len(apps),
            "truncated": len(apps) > MAX_APPS,
            "next_step": "Pick one app and call cua_get_app with its name, path or pid."}
