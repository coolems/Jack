"""Time Tools - Dynamic Entry Point

This module provides time and date functionality.
Each function is in its own file for maintainability.
"""

import logging
import os
import importlib
import datetime
import time
import pytz
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.Time")


class TimeTools:
    """Tools for getting current time and timezone information"""
    
    def __init__(self):
        self.system_timezone = self._detect_system_timezone()
        
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"TimeTools initialized with {len(self._tools)} tools")
    
    def _detect_system_timezone(self) -> str:
        """Detect the system's current timezone"""
        try:
            local_tz = datetime.datetime.now().astimezone().tzinfo
            if local_tz:
                return str(local_tz)
            if hasattr(time, 'tzname'):
                return time.tzname[0] if time.daylight else time.tzname[0]
            return "UTC"
        except Exception as e:
            logger.warning(f"Could not detect system timezone: {e}")
            return "UTC"
    
    def _discover_tools(self) -> None:
        """Auto-discover all tool functions in this directory"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        for filename in os.listdir(current_dir):
            if not filename.endswith('.py'):
                continue
            if filename == '__init__.py':
                continue
            
            tool_name = filename[:-3]
            module_path = f"tools.time_tools.{tool_name}"
            
            try:
                module = importlib.import_module(module_path)
                tool_func = getattr(module, tool_name, None)
                
                if tool_func and callable(tool_func):
                    # Bind system_timezone
                    def make_bound_wrapper(func, tz):
                        def wrapper(*args, **kwargs):
                            return func(*args, system_timezone=tz, **kwargs)
                        return wrapper
                    
                    self._tools[tool_name] = make_bound_wrapper(tool_func, self.system_timezone)
                    logger.debug(f"Discovered time_tool: {tool_name}")
                    
            except Exception as e:
                logger.error(f"Failed to load time_tool '{tool_name}': {e}")
    
    def get_all_tools(self) -> Dict[str, Callable]:
        return self._tools.copy()
    
    def get_tool_count(self) -> int:
        return len(self._tools)
    
    def __getattr__(self, name: str) -> Callable:
        if name in self._tools:
            return self._tools[name]
        raise AttributeError(f"TimeTools has no tool named '{name}'")
