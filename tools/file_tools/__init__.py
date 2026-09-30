"""File Tools - Dynamic Entry Point

This module provides file operations.
Each function is in its own file for maintainability.
"""

import logging
import os
import importlib
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.Files")


class FileTools:
    """File operations: read, write, list, rename, move, delete files ... """
    
    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"FileTools initialized with {len(self._tools)} tools")
    
    def _discover_tools(self) -> None:
        """Auto-discover all tool functions in this directory"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        for filename in os.listdir(current_dir):
            if not filename.endswith('.py'):
                continue
            if filename == '__init__.py':
                continue
            
            tool_name = filename[:-3]
            module_path = f"tools.file_tools.{tool_name}"
            
            try:
                module = importlib.import_module(module_path)
                tool_func = getattr(module, tool_name, None)
                
                if tool_func and callable(tool_func):
                    self._tools[tool_name] = tool_func
                    logger.debug(f"Discovered file_tool: {tool_name}")
                    
            except Exception as e:
                logger.error(f"Failed to load file_tool '{tool_name}': {e}")
    
    def get_all_tools(self) -> Dict[str, Callable]:
        """Return all discovered tools"""
        return self._tools.copy()
    
    def get_tool_count(self) -> int:
        return len(self._tools)
    
    def __getattr__(self, name: str) -> Callable:
        if name in self._tools:
            return self._tools[name]
        raise AttributeError(f"FileTools has no tool named '{name}'")