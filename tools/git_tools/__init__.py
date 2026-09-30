"""Git Tools - Dynamic Entry Point

This module provides git-related capabilities.
Each function is in its own file for maintainability.
Tools are auto-discovered at runtime - just add a .py file.
"""

import logging
import os
import importlib
from pathlib import Path
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.Git")


class GitTools:
    """Git operation tools - dynamically loaded from .py files in this folder"""

    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"GitTools initialized with {len(self._tools)} tools: {list(self._tools.keys())}")

    def _discover_tools(self) -> None:
        """
        Auto-discover all tool functions in this directory.
        Each .py file (except __init__.py and files in subfolders) should
        contain a function whose name matches the filename.
        """
        current_dir = Path(__file__).parent
        discovered = []

        for filepath in current_dir.iterdir():
            # Skip directories (including __pycache__, etc.)
            if not filepath.is_file():
                continue
            # Skip non-Python files
            if filepath.suffix != '.py':
                continue
            # Skip __init__.py itself
            if filepath.name == '__init__.py':
                continue

            tool_name = filepath.stem  # filename without .py
            module_path = f"tools.git_tools.{tool_name}"

            try:
                module = importlib.import_module(module_path)
                tool_func = getattr(module, tool_name, None)

                if tool_func and callable(tool_func):
                    self._tools[tool_name] = tool_func
                    discovered.append(tool_name)
                    logger.debug(f"Discovered git_tool: {tool_name}")
                else:
                    logger.warning(f"No callable '{tool_name}' found in {module_path}")

            except Exception as e:
                logger.error(f"Failed to load git_tool '{tool_name}': {e}")

    def get_all_tools(self) -> Dict[str, Callable]:
        """Return a copy of all discovered tools."""
        return self._tools.copy()

    def get_tool_count(self) -> int:
        """Return the number of discovered tools."""
        return len(self._tools)

    def __getattr__(self, name: str) -> Callable:
        """Allow direct access to tools as attributes."""
        if name in self._tools:
            return self._tools[name]
        raise AttributeError(f"GitTools has no tool named '{name}'")
