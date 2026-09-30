"""Drag function - drag mouse from current position to target coordinates"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "drag",
        "description": "Drag the mouse from its current position to specified coordinates while holding a mouse button. Useful for dragging windows, selecting text, or moving objects.",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {
                    "type": "integer",
                    "description": "Target X coordinate to drag to"
                },
                "y": {
                    "type": "integer",
                    "description": "Target Y coordinate to drag to"
                },
                "duration": {
                    "type": "number",
                    "description": "Duration of drag in seconds (default 0.5)",
                    "default": 0.5
                },
                "button": {
                    "type": "string",
                    "description": "Mouse button to hold during drag: 'left', 'right', or 'middle' (default 'left')",
                    "default": "left"
                }
            },
            "required": ["x", "y"]
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.MouseInteract")


def drag(x: int, y: int, duration: float = 0.5, button: str = 'left') -> str:
    """Drag mouse from current position to target coordinates."""
    try:
        import pyautogui
        start_x, start_y = pyautogui.position()
        
        # Calculate relative distance for drag()
        rel_x = x - start_x
        rel_y = y - start_y
        
        pyautogui.drag(rel_x, rel_y, duration=duration, button=button)
        return f"Dragged from ({start_x}, {start_y}) to ({x}, {y}) with '{button}' button"
    except ImportError:
        return "Error: PyAutoGUI not installed. Run: pip install pyautogui"
    except Exception as e:
        return f"ERROR: Drag failed: {str(e)}"
