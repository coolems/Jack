"""
Visual Browser Navigation - Dynamic Entry Point

This module provides visual mouse navigation capabilities.
Uses vision AI to find UI elements on browser screenshots, then moves
the mouse cursor to them and validates the position automatically.
"""

import logging
import os
import importlib
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.VisualBrowserNavigation")


class VisualBrowserNavigation:
    """
    Visual mouse navigation for browser automation.
    
    Uses vision AI to locate UI elements on browser screenshots,
    moves the mouse cursor to targets, and validates positioning.
    """
    
    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"VisualBrowserNavigation initialized with {len(self._tools)} tools")
    
    def _discover_tools(self) -> None:
        """Auto-discover all tool functions in this directory"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        for filename in os.listdir(current_dir):
            if not filename.endswith('.py'):
                continue
            if filename == '__init__.py':
                continue
            
            tool_name = filename[:-3]
            module_path = f"tools.visual_browser_navigation.{tool_name}"
            
            try:
                module = importlib.import_module(module_path)
                func = getattr(module, tool_name, None)
                
                if func and callable(func):
                    self._tools[tool_name] = func
                    logger.debug(f"Discovered visual_browser_navigation tool: {tool_name}")
                    
            except Exception as e:
                logger.error(f"Failed to load visual_browser_navigation tool '{tool_name}': {e}")
    
    def get_all_tools(self) -> Dict[str, Callable]:
        """Return all discovered tools"""
        return self._tools.copy()
    
    def get_tool_count(self) -> int:
        return len(self._tools)
    
    def __getattr__(self, name: str) -> Callable:
        if name in self._tools:
            return self._tools[name]
        raise AttributeError(f"VisualBrowserNavigation has no tool named '{name}'")
