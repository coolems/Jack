"""Scroll up function - scroll mouse wheel up"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "scroll_up",
        "description": "Scroll the mouse wheel up at the current cursor position or at specified coordinates.",
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


def scroll_up(amount: int = 3, x: int = None, y: int = None) -> str:
    """Scroll mouse wheel up."""
    try:
        import pyautogui
        if x is not None and y is not None:
            pyautogui.moveTo(x, y, duration=0.1)
        
        pyautogui.scroll(amount)
        pos = pyautogui.position()
        return f"Scrolled UP {amount} clicks at ({pos[0]}, {pos[1]})"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"ERROR: Scroll up failed: {str(e)}"
