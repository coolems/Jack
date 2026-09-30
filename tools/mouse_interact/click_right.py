"""Right click function - perform right mouse button click"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "click_right",
        "description": "Perform a right mouse button click (context menu) at the current cursor position or at specified coordinates.",
        "parameters": {
            "type": "object",
            "properties": {
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


def click_right(x: int = None, y: int = None) -> str:
    """Perform right mouse button click."""
    try:
        import pyautogui
        if x is not None and y is not None:
            pyautogui.moveTo(x, y, duration=0.1)
        
        pyautogui.click(button='right')
        pos = pyautogui.position()
        return f"Right button click at ({pos[0]}, {pos[1]})"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"ERROR: Right click failed: {str(e)}"
