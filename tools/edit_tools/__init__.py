"""Edit Tools - Dynamic Entry Point

Precise in-place patches for existing files (added 2026-11-06, ported from the deepseek-harness
`edit` / `str_replace_editor` tools). Each function lives in its own file for maintainability.

Import policy: this module imports ONLY stdlib. A bare 'from . import X' would be shipped to the
CLIENT as a '__init__' shared_dep and exec'd without the tool's own globals, so keep it import-free
(the same shape as tools/file_tools/__init__.py).
"""

import logging
import os
import importlib
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.Edit")


class EditTools:
    """File patching: literal text replacement and line insertion."""

    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"EditTools initialized with {len(self._tools)} tools")

    def _discover_tools(self) -> None:
        """Auto-discover all tool functions in this directory.

        Underscore-prefixed modules (e.g. '_edit_core') are shared logic, not tools, so they
        are skipped here - the tool modules import them directly.
        """
        current_dir = os.path.dirname(os.path.abspath(__file__))

        for filename in os.listdir(current_dir):
            if not filename.endswith('.py'):
                continue
            if filename == '__init__.py' or filename.startswith('_'):
                continue

            tool_name = filename[:-3]
            module_path = f"tools.edit_tools.{tool_name}"

            try:
                module = importlib.import_module(module_path)
                tool_func = getattr(module, tool_name, None)

                if tool_func and callable(tool_func):
                    self._tools[tool_name] = tool_func
                    logger.debug(f"Discovered edit_tool: {tool_name}")

            except Exception as e:
                logger.error(f"Failed to load edit_tool '{tool_name}': {e}")

    def get_all_tools(self) -> Dict[str, Callable]:
        """Return all discovered tools"""
        return self._tools.copy()

    def get_tool_count(self) -> int:
        return len(self._tools)

    def __getattr__(self, name: str) -> Callable:
        if name in self._tools:
            return self._tools[name]
        raise AttributeError(f"EditTools has no tool named '{name}'")
