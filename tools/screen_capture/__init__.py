"""
Screen Capture - Full desktop screenshot capability

This module captures the entire desktop screen (not just the browser window).
Combined with view_image, this gives the AI full screen vision.
"""

import logging
import os
import importlib
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.ScreenCapture")


class ScreenCapture:
    """
    Full desktop screen capture using PyAutoGUI.
    Takes screenshots of the entire screen for AI vision analysis.
    """
    
    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"ScreenCapture initialized with {len(self._tools)} tools")
    
    def _discover_tools(self) -> None:
        """Auto-discover all tool functions in this directory"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        for filename in os.listdir(current_dir):
            if not filename.endswith('.py'):
                continue
            if filename == '__init__.py':
                continue
            
            tool_name = filename[:-3]
            module_path = f"tools.screen_capture.{tool_name}"
            
            try:
                module = importlib.import_module(module_path)
                func = getattr(module, tool_name, None)
                
                if func and callable(func):
                    self._tools[tool_name] = func
                    logger.debug(f"Discovered screen_capture tool: {tool_name}")
                    
            except Exception as e:
                logger.error(f"Failed to load screen_capture tool '{tool_name}': {e}")
    
    def get_all_tools(self) -> Dict[str, Callable]:
        """Return all discovered tools"""
        return self._tools.copy()
    
    def get_tool_count(self) -> int:
        return len(self._tools)
