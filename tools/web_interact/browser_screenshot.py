"""Browser Screenshot - captures only the Chrome browser window during web browsing. Browser is in debug mode and we connect to it via CDP. Includes title bar, tabs, address bar, and borders."""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "browser_screenshot",
        "description": "Take a screenshot of the Chrome browser window only. Use this while browsing the internet since the browser is in debug mode and we connect to it via CDP. Saves as PNG to the working_root folder. Returns the Chrome window title, absolute position on screen, and mouse position. Automatically draws a red circle on the mouse cursor position. Returns the image data so you can see what's on screen directly.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Optional filename for screenshot (auto-generates if omitted)"}
            },
            "required": []
        }
    }
}

import logging
import os
import datetime
import base64
import ctypes
from ctypes import wintypes

from ..utils import get_working_root, validate_filename

logger = logging.getLogger("COOLEMS.Tools.WebInteract")

# Win32 API setup
GetWindowRect = ctypes.windll.user32.GetWindowRect
GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_int * 4)]
GetWindowRect.restype = wintypes.BOOL

GetWindowText = ctypes.windll.user32.GetWindowTextW
GetWindowText.argtypes = [wintypes.HWND, wintypes.LPWSTR, wintypes.INT]
GetWindowText.restype = wintypes.INT

EnumWindows = ctypes.windll.user32.EnumWindows
EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
EnumWindows.restype = wintypes.BOOL

GetCursorPos = ctypes.windll.user32.GetCursorPos
GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
GetCursorPos.restype = wintypes.BOOL

def _get_mouse_position():
    """Get current mouse cursor position in absolute screen coordinates."""
    point = wintypes.POINT()
    if GetCursorPos(ctypes.byref(point)):
        return {"absolute_x": point.x, "absolute_y": point.y}
    return None

def _find_window_by_title(page_title):
    """
    Find the Win32 HWND of the Chrome window that matches the page title.
    Returns the FULL window rectangle (including title bar, tabs, address bar, borders).
    """
    found = [None]

    def enum_proc(hwnd, lParam):
        if found[0] or not hwnd or not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True

        title_buffer = ctypes.create_unicode_buffer(512)
        GetWindowText(hwnd, title_buffer, 512)
        title = title_buffer.value

        if 'chrome' not in title.lower() and 'browser' not in title.lower():
            return True

        if page_title and page_title in title:
            rect = ctypes.c_int * 4
            r = rect()
            if GetWindowRect(hwnd, r):
                left, top, right, bottom = r[0], r[1], r[2], r[3]
                found[0] = {
                    "hwnd": hwnd,
                    "title": title,
                    "absolute_x": left,
                    "absolute_y": top,
                    "width": right - left,
                    "height": bottom - top,
                    "center_x": left + (right - left) // 2,
                    "center_y": top + (bottom - top) // 2,
                    "absolute_right": right,
                    "absolute_bottom": bottom
                }
                return False
        return True

    EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(enum_proc), 0)
    return found[0]

def _draw_cursor_marker(screenshot_img, mouse_abs_x, mouse_abs_y, win_left, win_top):
    """
    Draw a red circle on the screenshot at the mouse cursor position.
    
    Args:
        screenshot_img: PIL Image object
        mouse_abs_x: Absolute screen X of mouse
        mouse_abs_y: Absolute screen Y of mouse
        win_left: Absolute screen X of window top-left
        win_top: Absolute screen Y of window top-left
    
    Returns:
        Modified PIL Image with red circle drawn
    """
    try:
        from PIL import ImageDraw

        draw = ImageDraw.Draw(screenshot_img)
        img_width, img_height = screenshot_img.size

        # Calculate relative position within the captured window
        rel_x = mouse_abs_x - win_left
        rel_y = mouse_abs_y - win_top

        # Only draw if cursor is within the screenshot bounds
        if 0 <= rel_x < img_width and 0 <= rel_y < img_height:
            # Draw a red circle around the cursor position
            circle_radius = 20
            circle_radiusW = 18
            circle_radiusW2 = 5
            draw.ellipse(
                [rel_x - circle_radiusW, rel_y - circle_radiusW,
                 rel_x + circle_radiusW, rel_y + circle_radiusW],
                outline="white",
                width=3
            )
            draw.ellipse(
                [rel_x - circle_radiusW2, rel_y - circle_radiusW2,
                 rel_x + circle_radiusW2, rel_y + circle_radiusW2],
                outline="white",
                width=2
            )
            draw.ellipse(
                [rel_x - circle_radius, rel_y - circle_radius,
                 rel_x + circle_radius, rel_y + circle_radius],
                outline="red",
                width=2
            )

            # Draw crosshair lines
            line_length = 8
            draw.line(
                [(rel_x - line_length, rel_y), (rel_x + line_length, rel_y)],
                fill="red",
                width=2
            )
            draw.line(
                [(rel_x, rel_y - line_length), (rel_x, rel_y + line_length)],
                fill="red",
                width=2
            )

        return screenshot_img

    except Exception as e:
        logger.warning(f"Could not draw cursor marker on screenshot: {e}")
        return screenshot_img

async def browser_screenshot(filename: str = None) -> dict:
    """Take a Chrome browser window screenshot (including title bar, tabs, address bar, borders) with red circle cursor marker and return the CORRECT Chrome window info + mouse absolute screen position + image data for vision."""
    from .ensure_connected import ensure_connected
    from .shared_instance import get_web_interact

    if not await ensure_connected():
        return {"error": "Not connected to Chrome.", "image_base64": None}

    from .shared_instance import ensure_page
    await ensure_page()  # self-heal: re-acquire or create a live page (2026-09-14 alias fix: CLIENT transform neutralizes same-package imports; aliases are never injected into exec globals -> NameError)
    wi = get_web_interact()
    if not wi or wi.page is None:
        return {"error": "No page available.", "image_base64": None}

    try:
        # Generate filename
        if not filename:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{timestamp}.png"
        else:
            # SECURITY (2026-07-15): user-supplied filenames must stay inside
            # working_root -- strip any directory part, reject traversal.
            try:
                validate_filename(filename, label="Screenshot filename")
            except ValueError as e:
                return {"error": f"Screenshot failed: {e}", "image_base64": None}
        if not filename.endswith('.png'):
            filename += '.png'

        working_dir = get_working_root()
        os.makedirs(working_dir, exist_ok=True)
        filepath = os.path.normpath(os.path.join(working_dir, filename))

        # Final containment check -- belt and braces (realpath resolves symlinks/..)
        from ..utils import guard_path_inside_working_root
        try:
            guard_path_inside_working_root(filepath, working_dir, label="Screenshot file path")
        except ValueError as e:
            return {"error": f"Screenshot failed: {e}", "image_base64": None}

        # Get page info BEFORE screenshot
        page_title = await wi.page.title()
        page_url = wi.page.url

        # Find the CORRECT Win32 window by matching page title
        # This gives us the FULL window rectangle (including title bar, tabs, address bar, borders)
        matching_window = _find_window_by_title(page_title)

        # Get mouse position
        mouse_pos = _get_mouse_position()

        if matching_window is None:
            # Fallback: if we can't find the window by title, take a regular Playwright screenshot
            logger.warning("Could not find matching Win32 window, falling back to Playwright screenshot")
            screenshot_bytes = await wi.page.screenshot()
            with open(filepath, 'wb') as f:
                f.write(screenshot_bytes)

            file_size = os.path.getsize(filepath)

            # Encode image to base64 for vision
            with open(filepath, 'rb') as f:
                image_base64 = base64.b64encode(f.read()).decode('utf-8')

            return {
                "image_base64": image_base64,
                "screenshot_path": filepath,
                "file_size": file_size,
                "status": "success (fallback - viewport only)",
                "page": {
                    "title": page_title,
                    "url": page_url
                },
                "browser_window": {
                    "title": page_title,
                    "error": "Could not find matching Win32 window for this page"
                }
            }

        # Capture the FULL window using pyautogui with the exact window rectangle
        try:
            import pyautogui

            region = (
                matching_window["absolute_x"],
                matching_window["absolute_y"],
                matching_window["width"],
                matching_window["height"]
            )

            screenshot_img = pyautogui.screenshot(region=region)

            if screenshot_img.width == 0 or screenshot_img.height == 0:
                return {
                    "status": "error",
                    "message": "Screenshot captured but has zero dimensions.",
                    "window_info": matching_window,
                    "image_base64": None
                }

            # Draw cursor marker on the screenshot
            if mouse_pos:
                screenshot_img = _draw_cursor_marker(
                    screenshot_img,
                    mouse_pos["absolute_x"],
                    mouse_pos["absolute_y"],
                    matching_window["absolute_x"],
                    matching_window["absolute_y"]
                )

            # Save to file
            screenshot_img.save(filepath, "PNG")
            file_size = os.path.getsize(filepath)

        except ImportError:
            return {
                "error": "pyautogui is not installed. Install with: pip install pyautogui",
                "image_base64": None
            }
        except Exception as e:
            logger.warning(f"pyautogui capture failed: {e}")
            return {
                "status": "error",
                "message": f"Failed to capture window: {str(e)}",
                "window_info": matching_window,
                "image_base64": None
            }

        # Encode image to base64 for vision
        with open(filepath, 'rb') as f:
            image_base64 = base64.b64encode(f.read()).decode('utf-8')

        # Build result
        result = {
            "image_base64": image_base64,
            "screenshot_path": filepath,
            "file_size": file_size,
            "status": "success",
            "capture_method": "pyautogui_full_window",
            "page": {
                "title": page_title,
                "url": page_url
            }
        }

        result["browser_window"] = {
            "hwnd": matching_window["hwnd"],
            "title": matching_window["title"],
            "Window Absolute Position": {
                "x": matching_window["absolute_x"],
                "y": matching_window["absolute_y"]
            },
            "size": {
                "width": matching_window["width"],
                "height": matching_window["height"]
            },
            "center": {
                "x": matching_window["center_x"],
                "y": matching_window["center_y"]
            },
            "edges": {
                "right": matching_window["absolute_right"],
                "bottom": matching_window["absolute_bottom"]
            }
        }

        # Add mouse absolute position
        if mouse_pos:
            result["Mouse Absolute Position"] = {
                "x": mouse_pos["absolute_x"],
                "y": mouse_pos["absolute_y"]
            }
        else:
            result["Mouse Absolute Position"] = "Could not detect mouse cursor position"

        return result

    except Exception as e:
        return {
            "error": f"Screenshot failed: {str(e)}",
            "image_base64": None
        }
