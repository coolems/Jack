"""Left click function - perform left mouse button click"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "click_left",
        "description": "Perform a left mouse button click at the current cursor position or at specified coordinates. Supports single and double clicks.",
        "parameters": {
            "type": "object",
            "properties": {
                "clicks": {
                    "type": "integer",
                    "description": "Number of clicks: 1=single click, 2=double click (default 1)",
                    "default": 1
                },
                "x": {
                    "type": "integer",
                    "description": "X coordinate to click at (optional, uses current position if not provided)"
                },
                "y": {
                    "type": "integer",
                    "description": "Y coordinate to click at (optional, uses current position if not provided)"
                }
            },
            "required": []
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.MouseInteract")


def click_left(clicks: int = 1, x: int = None, y: int = None) -> str:
    """Perform left mouse button click."""
    try:
        import pyautogui
        if x is not None and y is not None:
            pyautogui.moveTo(x, y, duration=0.1)
        
        pyautogui.click(clicks=clicks, button='left')
        pos = pyautogui.position()
        click_type = "double" if clicks == 2 else "single"
        return f"Left button {click_type}-click at ({pos[0]}, {pos[1]})"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"ERROR: Left click failed: {str(e)}"
