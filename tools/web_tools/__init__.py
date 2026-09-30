"""
Web Tools - Dynamic Entry Point

This module auto-discovers all tool functions in the web_tools folder.
NO Ollama dependencies - all tools are pure functions.
"""

import logging
import os
import importlib
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.Web")


class WebTools:
    """
    Dynamic web tools wrapper - auto-discovers ALL functions in this folder.
    Each tool function remains in its own file with its __tool_description__.
    This class only provides discovery and access - no function logic here.
    """
    
    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"WebTools initialized with {len(self._tools)} tools: {list(self._tools.keys())}")
    
    def _discover_tools(self) -> None:
        """Auto-discover all tool functions in this directory"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        for filename in os.listdir(current_dir):
            if not filename.endswith('.py'):
                continue
            if filename == '__init__.py':
                continue
            
            tool_name = filename[:-3]
            module_path = f"tools.web_tools.{tool_name}"
            
            try:
                module = importlib.import_module(module_path)
                tool_func = getattr(module, tool_name, None)
                
                if tool_func and callable(tool_func):
                    self._tools[tool_name] = tool_func
                    logger.debug(f"Discovered web_tool: {tool_name}")
                else:
                    logger.warning(f"No function named '{tool_name}' in {filename}")
                    
            except Exception as e:
                logger.error(f"Failed to load web_tool '{tool_name}': {e}")
    
    def get_all_tools(self) -> Dict[str, Callable]:
        """Return all discovered tools"""
        return self._tools.copy()
    
    def get_tool_names(self) -> list:
        """Return sorted list of tool names"""
        return sorted(self._tools.keys())
    
    def get_tool_count(self) -> int:
        """Return number of discovered tools"""
        return len(self._tools)
    
    def __getattr__(self, name: str) -> Callable:
        """Dynamic attribute access"""
        if name in self._tools:
            return self._tools[name]
        raise AttributeError(f"WebTools has no tool named '{name}'")
    
    def __dir__(self) -> list:
        """Make tools visible for tab completion"""
        return list(super().__dir__()) + list(self._tools.keys())
