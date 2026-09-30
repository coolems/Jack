"""Execution Tools - Dynamic Entry Point

This module provides code execution capabilities (Python, and future languages).
Each function is in its own .py file for maintainability.
Tools are auto-discovered at runtime - just add a .py file to this folder.

Example:
    - Add cpp_exec.py with a function named `cpp_exec` -> it's automatically loaded
    - Remove a file -> it's automatically excluded on next run

Note: calculator.py has been moved to _old/ and is no longer active.
      Use python_exec for all calculations instead.
"""

import logging
import os
import importlib
from pathlib import Path
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.Exec")

# Folders to exclude from tool discovery


class ExecTools:
    """
    Execution tools - dynamically loaded from .py files in this folder.

    Current tools:
        - python_exec: Execute arbitrary Python code

    To add a new tool:
        1. Create a new .py file (e.g., cpp_exec.py)
        2. Define a function with the same name (def cpp_exec(...): ...)
        3. Include __tool_description__ dict for the tool schema
        4. Restart - the tool is auto-discovered
    """

    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"ExecTools initialized with {len(self._tools)} tools: {list(self._tools.keys())}")

    def _discover_tools(self) -> None:
        """
        Auto-discover all tool functions in this directory.

        Rules:
        - Only files in the immediate folder (not subdirectories) are scanned
        - Folders starting with _ or __ are excluded (e.g., _old, __pycache__)
        - Files starting with '_' are guardrail/infra helpers of python_exec.py
          (_policy.py, _ast_guard.py, _pattern_scan.py) -- NOT tools; skipped.
        - Each .py file must contain a callable whose name matches the filename
        """
        current_dir = Path(__file__).parent
        discovered = []

        for filepath in current_dir.iterdir():
            # Skip directories (including _old, __pycache__, etc.)
            if not filepath.is_file():
                continue
            # Skip non-Python files
            if filepath.suffix != '.py':
                continue
            # Skip __init__.py itself and underscore-prefixed guardrail helpers
            # (_policy.py / _ast_guard.py / _pattern_scan.py support python_exec)
            if filepath.name == '__init__.py' or filepath.stem.startswith('_'):
                continue

            tool_name = filepath.stem  # filename without .py
            module_path = f"tools.exec_tools.{tool_name}"

            try:
                module = importlib.import_module(module_path)
                tool_func = getattr(module, tool_name, None)

                if tool_func and callable(tool_func):
                    self._tools[tool_name] = tool_func
                    discovered.append(tool_name)
                    logger.debug(f"Discovered exec_tool: {tool_name}")
                else:
                    logger.warning(f"No callable '{tool_name}' found in {module_path}")

            except Exception as e:
                logger.error(f"Failed to load exec_tool '{tool_name}': {e}")

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
        raise AttributeError(f"ExecTools has no tool named '{name}'")
