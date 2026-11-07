"""cua_capabilities - read-only desktop capability preflight (no input, no dialog).

Ported from the harness's getCapabilities: report what this machine can actually do
BEFORE attempting any desktop action, and never open a prompt or send input here.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_capabilities",
        "description": "Read-only desktop preflight: Windows + UI Automation availability, screen lock, screen size, cursor, live handle counts and per-app grants. Sends no input, opens no dialog - call it first. A false value never implies consent.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

import logging

from ._desktop_win import desktop_available, cua_screen_size, cua_session_locked, cua_foreground_hwnd
from ._desktop_input import cua_cursor_pos
from ._desktop_uia import uia_available
from ._desktop_state import window_identity_summary
from ._consent import consent_summary

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Capabilities")


def cua_capabilities() -> dict:
    """Report desktop capabilities for the current process."""
    if not desktop_available():
        return {"status": "unavailable",
                "reason": "Win32 desktop APIs are not available in this process (non-Windows host).",
                "desktop_available": False, "uia_available": False,
                "session_locked": True, "screen": None, "cursor": None,
                "tracked": window_identity_summary(), "access": consent_summary()}

    locked = cua_session_locked()
    screen = cua_screen_size()
    cursor = cua_cursor_pos()
    return {
        "status": "ok",
        "platform": "windows",
        "desktop_available": True,
        "uia_available": uia_available(),
        "session_locked": locked,
        "screen": {"width": screen[0], "height": screen[1]},
        "cursor": {"x": cursor[0], "y": cursor[1]} if cursor else None,
        "foreground_hwnd": cua_foreground_hwnd(),
        "tracked": window_identity_summary(),
        "access": consent_summary(),
        "coordinate_space": "window-local pixels measured from the top-left of the latest cua_screenshot of that window; relative = thousandths 0..999",
        "next_step": ("Screen is locked - ask the user to unlock it before any desktop action."
                      if locked else "Use cua_list_apps, then cua_get_app to bind an app handle."),
    }
