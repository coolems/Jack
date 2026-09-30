"""Middle click function - perform middle mouse button click"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "click_middle",
        "description": "Perform a middle mouse button click at the current cursor position or at specified coordinates. Often used to open links in new tabs.",
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


def click_middle(x: int = None, y: int = None) -> str:
    """Perform middle mouse button click."""
    try:
        import pyautogui
        if x is not None and y is not None:
            pyautogui.moveTo(x, y, duration=0.1)
        
        pyautogui.click(button='middle')
        pos = pyautogui.position()
        return f"Middle button click at ({pos[0]}, {pos[1]})"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"ERROR: Middle click failed: {str(e)}"
