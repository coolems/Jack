"""cua_get_app - bind an app handle to ONE app the user approved.

Ported from getApp. The selector the user was shown on the consent card IS the consent
key stored on the handle (consent_identity), so a handle can never cover a different app
than the one approved. Nothing is launched here: only already-running processes are
bound, and no window is activated - binding reads identity and window rectangles only.

Passing app_handle instead of selector re-enumerates an ALREADY GRANTED app's windows
(no new card): a window opened after the first bind becomes addressable, and a revoked
handle fails instead of silently acting on another app.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_get_app",
        "description": "Bind an app handle for a RUNNING app by name, exe path or pid. Asks the user's per-app desktop consent; returns app_handle, pid, exe_path, windows. Launches and activates nothing; a denied app stays denied. Pass app_handle alone to refresh an already-granted app's windows.",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "App name, exe path, or pid as digits."},
                "app_handle": {"type": "string", "description": "Existing handle to refresh its windows (keeps the grant)."}
            },
            "required": []
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_enum_windows, cua_process_image_path
from ._desktop_state import (app_consent_key, consent_identity, get_app, new_handle_token,
                             register_app, register_window, rect_is_minimized)
from ._consent import current_conversation_id, is_granted, was_denied

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.GetApp")


def _matches(selector: str, identity: str, pid: int) -> bool:
    """Whether one running window belongs to the requested selector."""
    key = consent_identity(selector)
    if key.startswith("pid:"):
        return pid == int(key[4:])
    if "\\" in key or "/" in key or key[1:2] == ":":
        return identity.lower() == key.lower()
    return identity.lower() == key.lower() or key in identity.lower()


def _rebind(app_handle: str) -> dict:
    """Re-enumerate an ALREADY GRANTED app's windows (no new card)."""
    record = get_app(app_handle)
    if record is None:
        return {"status": "error",
                "error": (f"APP_REQUIRED: app_handle {app_handle} is unknown or was revoked. "
                          "Call cua_get_app with the app name again.")}
    if not is_granted(app_consent_key(app_handle), current_conversation_id()):
        return {"status": "error",
                "error": "DESKTOP_CONSENT_REQUIRED: access to this app is not granted."}

    out = []
    for w in cua_enum_windows():
        if w["pid"] != record["pid"]:
            continue
        window_id = new_handle_token()
        register_window(window_id, w["hwnd"], app_handle, w["rect"], title=w["title"])
        out.append({"windowId": window_id, "title": w["title"], "hwnd": w["hwnd"],
                    "rect": w["rect"], "on_screen": not rect_is_minimized(w["rect"])})
    if not out:
        return {"status": "error",
                "error": (f"APP_NOT_RUNNING: app {record['app_name']!r} (pid {record['pid']}) "
                          "has no visible window any more.")}
    return {"status": "ok", "app_handle": app_handle, "app": record["app_name"],
            "pid": record["pid"], "exe_path": record["exe_path"], "windows": out,
            "next_step": "Use a windowId with cua_get_state or cua_screenshot before any action."}


def cua_get_app(selector: str = None, app_handle: str = None) -> dict:
    """Bind an app handle for a running app."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process."}
    if app_handle is not None:
        return _rebind(app_handle)
    if not isinstance(selector, str) or not selector.strip():
        return {"status": "error",
                "error": "INVALID_SELECTOR: give an app name, executable path, or pid."}

    conv_id = current_conversation_id()
    key = consent_identity(selector)
    if not key:
        return {"status": "error", "error": "INVALID_SELECTOR: could not derive an app identity."}

    if not conv_id:
        return {"status": "error",
                "error": ("NO_CONSENT_CHANNEL: this call has no chat to ask the local user through, "
                          "so desktop access fails closed.")}
    if was_denied(key, conv_id):
        return {"status": "error",
                "error": ("DESKTOP_ACCESS_DENIED: the user refused access to this app for this "
                          "session. Do not retry it and do not switch tools to bypass the refusal.")}
    if not is_granted(key, conv_id):
        return {"status": "error",
                "error": ("DESKTOP_CONSENT_REQUIRED: the local user has not allowed this app yet. "
                          "Call cua_get_app again - the approval card is shown by the client.")}

    windows = cua_enum_windows()
    matched = []
    exe_path = ""
    pid = 0
    for w in windows:
        candidate_exe = cua_process_image_path(w["pid"])
        if _matches(selector, w["title"], w["pid"]) or (candidate_exe and _matches(selector, candidate_exe, w["pid"])):
            pid = w["pid"]
            exe_path = candidate_exe or exe_path
            matched.append(w)

    if not matched:
        return {"status": "error",
                "error": (f"APP_NOT_RUNNING: no visible window matches {selector!r}. "
                          "Use cua_list_apps to see what is running - this tool never launches an app.")}

    app_handle = new_handle_token()
    register_app(app_handle, pid, exe_path, matched[0]["title"], consent_key=key)

    out = []
    for w in matched:
        window_id = new_handle_token()
        register_window(window_id, w["hwnd"], app_handle, w["rect"], title=w["title"])
        out.append({"windowId": window_id, "title": w["title"], "hwnd": w["hwnd"],
                    "rect": w["rect"], "on_screen": not rect_is_minimized(w["rect"])})

    logger.info("[CUA] bound app %s pid=%s windows=%d", key, pid, len(out))
    return {"status": "ok", "app_handle": app_handle, "app": matched[0]["title"],
            "pid": pid, "exe_path": exe_path, "windows": out,
            "next_step": "Use a windowId with cua_get_state or cua_screenshot before any action."}
