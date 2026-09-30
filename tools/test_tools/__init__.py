"""Test Tools - Internal testing framework for verifying tool health.

This module provides testing capabilities to verify that all tools
are working correctly after code changes or refactoring.

Tools:
    - run_tests: Run the test suite and report results
"""

import logging
import os
import importlib
from pathlib import Path
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.Test")

class TestTools:
    """
    Testing tools - dynamically loaded from .py files in this folder.
    """

    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"TestTools initialized with {len(self._tools)} tools: {list(self._tools.keys())}")

    def _discover_tools(self) -> None:
        """Auto-discover all test tool functions in this directory."""
        current_dir = Path(__file__).parent
        discovered = []

        for filepath in current_dir.iterdir():
            if not filepath.is_file():
                continue
            if filepath.suffix != '.py':
                continue
            if filepath.name == '__init__.py':
                continue

            tool_name = filepath.stem
            module_path = f"tools.test_tools.{tool_name}"

            try:
                module = importlib.import_module(module_path)
                tool_func = getattr(module, tool_name, None)

                if tool_func and callable(tool_func):
                    self._tools[tool_name] = tool_func
                    discovered.append(tool_name)
                    logger.debug(f"Discovered test_tool: {tool_name}")
            except Exception as e:
                logger.error(f"Failed to load test_tool '{tool_name}': {e}")

    def get_all_tools(self) -> Dict[str, Callable]:
        """Return a copy of all discovered tools."""
        return self._tools.copy()
