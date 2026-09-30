"""Set mouse position relative function - move cursor relative to current position"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "set_pos_relative",
        "description": "Move the mouse cursor relative to its current position. Use positive values to move right/down, negative to move left/up.",
        "parameters": {
            "type": "object",
            "properties": {
                "dx": {
                    "type": "integer",
                    "description": "Change in X coordinate (positive=right, negative=left)"
                },
                "dy": {
                    "type": "integer",
                    "description": "Change in Y coordinate (positive=down, negative=up)"
                },
                "duration": {
                    "type": "number",
                    "description": "Duration of movement in seconds (default 0.2)",
                    "default": 0.2
                }
            },
            "required": ["dx", "dy"]
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.MouseInteract")


def set_pos_relative(dx: int, dy: int, duration: float = 0.2) -> str:
    """Move mouse relative to current position."""
    try:
        import pyautogui
        old_x, old_y = pyautogui.position()
        pyautogui.moveRel(dx, dy, duration=duration)
        new_x, new_y = pyautogui.position()
        return f"Mouse moved by ({dx}, {dy}) from ({old_x}, {old_y}) to ({new_x}, {new_y})"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"Error: Failed to move mouse: {str(e)}"
