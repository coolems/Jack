"""
Mouse Interact - System-level mouse control using pyautogui

This module provides direct mouse control on the desktop screen.
Works independently of the browser - controls the actual mouse cursor.
"""

import logging
import os
import importlib
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.MouseInteract")

# Global pyautogui instance
_pyautogui = None


def _get_pyautogui():
    """Lazy load pyautogui"""
    global _pyautogui
    if _pyautogui is None:
        try:
            import pyautogui
            _pyautogui = pyautogui
            # Set safe mode to prevent accidental interruptions
            pyautogui.FAILSAFE = True
            logger.info("PyAutoGUI initialized successfully")
        except ImportError:
            logger.error("PyAutoGUI not installed. Run: pip install pyautogui")
            raise
    return _pyautogui


class MouseInteract:
    """
    System-level mouse control using PyAutoGUI.
    Controls the actual mouse cursor on the desktop.
    """
    
    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"MouseInteract initialized with {len(self._tools)} tools")
    
    def _discover_tools(self) -> None:
        """Auto-discover all tool functions in this directory"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        for filename in os.listdir(current_dir):
            if not filename.endswith('.py'):
                continue
            if filename == '__init__.py':
                continue
            
            tool_name = filename[:-3]
            module_path = f"tools.mouse_interact.{tool_name}"
            
            try:
                module = importlib.import_module(module_path)
                func = getattr(module, tool_name, None)
                
                if func and callable(func):
                    self._tools[tool_name] = func
                    logger.debug(f"Discovered mouse_interact tool: {tool_name}")
                    
            except Exception as e:
                logger.error(f"Failed to load mouse_interact tool '{tool_name}': {e}")
    
    def get_all_tools(self) -> Dict[str, Callable]:
        """Return all discovered tools"""
        return self._tools.copy()
    
    def get_tool_count(self) -> int:
        return len(self._tools)
