"""cua_screenshot - capture ONE window, and define the coordinate space for it.

Ported from the harness's screenshot: pixels are window-local, measured from the
top-left of THIS capture, and the window rectangle is recorded so later absolute
coordinates are validated against it (WINDOW_CHANGED if the window moved/resized).

PrintWindow(RENDERFULLCONTENT) draws the window itself, so it works even when another
window overlaps it. If PrintWindow refuses (some GPU-composited windows) a screen-region
fallback is used and REPORTED - that fallback captures whatever is visible at that
rectangle, occlusion included, so the method is never hidden from the caller.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "cua_screenshot",
        "description": "Capture one window to a PNG in .temp/screenshots/ of working_root (scratch policy: screenshots never land in the repo root) and return image_base64 + its rect. Coordinates read from this image are window-local pixels (0,0 = top-left) and stay valid only while that rect is unchanged. Reports the capture method; the screen-region fallback includes occlusion.",
        "parameters": {
            "type": "object",
            "properties": {
                "window_id": {"type": "string", "description": "windowId from cua_get_app/cua_list_windows."},
                "filename": {"type": "string", "description": "Optional PNG filename in .temp/screenshots/ (auto-named if omitted)."}
            },
            "required": ["window_id"]
        }
    }
}

import base64
import datetime
import logging
import os

from ._desktop_win import desktop_available, cua_window_rect
from ._desktop_gdi import cua_capture_window
from ._desktop_state import get_window, rect_is_minimized, window_consent_key
from ._consent import current_conversation_id, is_granted

from ..utils import (get_working_root, validate_filename, guard_path_inside_working_root,
                     SCRATCH_SCREENSHOTS_DIR)

logger = logging.getLogger("COOLEMS.Tools.ComputerUse.Screenshot")


def _encode_png(raw: bytes, width: int, height: int) -> bytes:
    """RGB bytes -> PNG bytes. Pillow is a pinned dependency of this repo.

    _desktop_gdi already returns tightly packed RGB (stride padding removed, blue/red
    swapped), so this only encodes. Live check on 2026-11-07: the page's green #2f6f4f
    button now reads as (47,111,79) in the PNG, matching the browser's own screen grab.
    """
    from PIL import Image
    image = Image.frombytes("RGB", (width, height), raw)
    import io
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def cua_screenshot(window_id: str, filename: str = None) -> dict:
    """Capture one window's pixels and record the coordinate space."""
    if not desktop_available():
        return {"status": "error", "error": "Win32 desktop APIs are not available in this process.",
                "image_base64": None}

    window = get_window(window_id)
    if window is None:
        return {"status": "error",
                "error": (f"WINDOW_REQUIRED: window {window_id} is not registered. "
                          "Call cua_list_windows first."), "image_base64": None}
    if not is_granted(window_consent_key(window_id), current_conversation_id()):
        return {"status": "error", "error": "DESKTOP_CONSENT_REQUIRED: access to this app is not granted.",
                "image_base64": None}

    rect = cua_window_rect(window["hwnd"])
    if rect is None:
        return {"status": "error", "error": f"WINDOW_CHANGED: window {window_id} has no readable rectangle.",
                "image_base64": None}
    if rect_is_minimized(rect):
        return {"status": "error",
                "error": (f"WINDOW_MINIMIZED: window {window_id} is off-screen, so it has no pixels. "
                          "Use cua_show_window, then capture again."), "image_base64": None}

    raw, method = cua_capture_window(window["hwnd"], rect["width"], rect["height"],
                                     screen_rect=rect)
    if raw is None:
        return {"status": "error", "error": f"CAPTURE_FAILED: {method}", "image_base64": None}

    try:
        png = _encode_png(raw, rect["width"], rect["height"])
    except ImportError as e:
        return {"status": "error", "error": f"Pillow not installed ({e}). Run: pip install Pillow",
                "image_base64": None}

    if not filename:
        filename = f"cua_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    else:
        try:
            validate_filename(filename, label="Screenshot filename")
        except ValueError as e:
            return {"status": "error", "error": f"Screenshot failed: {e}", "image_base64": None}
    if not filename.endswith(".png"):
        filename += ".png"

    working_dir = get_working_root()
    # SCRATCH POLICY (2026-11-09, user request): screenshots are scratch artifacts, so
    # they land in <working_root>/.temp/screenshots/ - never in the working_root root.
    shot_dir = os.path.normpath(os.path.join(working_dir, SCRATCH_SCREENSHOTS_DIR))
    os.makedirs(shot_dir, exist_ok=True)
    filepath = os.path.normpath(os.path.join(shot_dir, filename))
    try:
        guard_path_inside_working_root(filepath, working_dir, label="Screenshot file path")
    except ValueError as e:
        return {"status": "error", "error": f"Screenshot failed: {e}", "image_base64": None}

    with open(filepath, "wb") as fh:
        fh.write(png)

    logger.info("[CUA] captured window %s %dx%d via %s", window_id, rect["width"], rect["height"], method)
    return {"status": "ok",
            "image_base64": base64.b64encode(png).decode("utf-8"),
            "screenshot_path": filepath,
            "file_size": os.path.getsize(filepath),
            "windowId": window_id, "rect": rect,
            "width": rect["width"], "height": rect["height"],
            "capture_method": method,
            "coordinate_space": "window-local pixels from the top-left of THIS image",
            "next_step": ("Act with x,y from this image, relative [0..999], or a snapshotId + "
                          "elementIndex. If the window moves, coordinates are rejected with WINDOW_CHANGED.")}
