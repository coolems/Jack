"""Scroll down function - scroll mouse wheel down"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "scroll_down",
        "description": "Scroll the mouse wheel down at the current cursor position or at specified coordinates.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "integer",
                    "description": "Number of scroll clicks (default 3)",
                    "default": 3
                },
                "x": {
                    "type": "integer",
                    "description": "X coordinate to scroll at (optional, uses current position if not provided)"
                },
                "y": {
                    "type": "integer",
                    "description": "Y coordinate to scroll at (optional, uses current position if not provided)"
                }
            },
            "required": []
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.MouseInteract")


def scroll_down(amount: int = 3, x: int = None, y: int = None) -> str:
    """Scroll mouse wheel down."""
    try:
        import pyautogui
        if x is not None and y is not None:
            pyautogui.moveTo(x, y, duration=0.1)
        
        pyautogui.scroll(-amount)
        pos = pyautogui.position()
        return f"Scrolled DOWN {amount} clicks at ({pos[0]}, {pos[1]})"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"ERROR: Scroll down failed: {str(e)}"
