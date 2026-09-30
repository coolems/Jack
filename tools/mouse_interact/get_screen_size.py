"""Get screen size function - returns screen dimensions"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "get_screen_size",
        "description": "Get the screen dimensions in pixels. Returns width and height of the primary display.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.MouseInteract")


def get_screen_size() -> str:
    """Get screen dimensions in pixels."""
    try:
        import pyautogui
        width, height = pyautogui.size()
        return f"Screen size: {width} x {height} pixels"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"ERROR: Failed to get screen size: {str(e)}"
