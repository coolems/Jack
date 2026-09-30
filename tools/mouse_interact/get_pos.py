"""Get mouse position function - returns current cursor coordinates"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "get_pos",
        "description": "Get current mouse cursor position on the screen. Returns X and Y coordinates.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.MouseInteract")


def get_pos() -> str:
    """Get current mouse cursor position on screen."""
    try:
        import pyautogui
        x, y = pyautogui.position()
        return f"Current mouse position: X={x}, Y={y}"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"ERROR: Failed to get mouse position: {str(e)}"
