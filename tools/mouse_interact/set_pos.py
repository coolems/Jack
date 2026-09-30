"""Set mouse position function - move cursor to exact coordinates"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "set_pos",
        "description": "Move the mouse cursor to exact screen coordinates. Use this to position the mouse before clicking.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {
                    "type": "integer",
                    "description": "X coordinate on screen (horizontal, 0=left)"
                },
                "y": {
                    "type": "integer",
                    "description": "Y coordinate on screen (vertical, 0=top)"
                },
                "duration": {
                    "type": "number",
                    "description": "Duration of movement in seconds (default 0.2). Higher values make the movement smoother/slower.",
                    "default": 0.2
                }
            },
            "required": ["x", "y"]
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.MouseInteract")


def set_pos(x: int, y: int, duration: float = 0.2) -> str:
    """Move mouse to exact screen coordinates."""
    try:
        import pyautogui
        screen_width, screen_height = pyautogui.size()
        
        if x < 0 or x > screen_width:
            return f"Error: X coordinate {x} out of range (0-{screen_width})"
        if y < 0 or y > screen_height:
            return f"Error: Y coordinate {y} out of range (0-{screen_height})"
        
        pyautogui.moveTo(x, y, duration=duration)
        return f"Mouse moved to ({x}, {y})"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"Error: Failed to move mouse: {str(e)}"
