"""Computer Use - Windows desktop control through UI Automation + Win32.

Ported from deepseek-harness's computer-use subsystem, adapted to this repo's
architecture (SERVER ships the source, CLIENT compiles and runs it locally).

Their design kept ONE tool with a {method, params} envelope because it wrapped an
external driver. Our tools are one callable per action (like every other folder in
tools/), but the SEMANTICS are ported:

  * app/window handles instead of blind screen pixels
  * UIA snapshot + element index, invalidated when the scope is re-observed
  * window-local coordinates tied to the screenshot/snapshot that produced them
    (WINDOW_CHANGED on resize, SCREENSHOT_REQUIRED when nothing was captured)
  * relative coordinates as thousandths of the window (0..999)
  * every action returns {'dispatched': True, 'verified': False} - a delivered
    click is NOT proof of an outcome; re-observe before concluding
  * per-application consent, fail-closed, and NO destructive verbs
    (no Close/kill/taskkill/shell anywhere in this folder)

The native layer is split by concern, one dep per file: _desktop_input (SendInput mouse +
keyboard), _desktop_win (windows, process identity, session lock, clipboard), _desktop_gdi
(pixel capture), _desktop_uia (UI Automation snapshot + pattern actions). Handle/snapshot/
coordinate state lives in _desktop_state and the user-consent relay in _consent. Tool files
import none of ctypes/comtypes directly - the AST guard therefore needs no per-tool
allowlist row for this folder (see tests/test_computer_use.py).
"""

import logging
import os
import importlib
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.ComputerUse")


class ComputerUse:
    """Windows desktop control tools (UI Automation + Win32 SendInput).

    Discovery follows the same convention as every other tools/ folder: every
    .py here that defines a top-level function named after the file becomes a
    tool. The underscore-prefixed helper modules (_desktop_input, _desktop_win,
    _desktop_gdi, _desktop_uia, _desktop_state, _consent) define no such function,
    so they are skipped by discovery and are delivered to the CLIENT as
    same-package shared_deps instead.
    """

    def __init__(self):
        self._tools: Dict[str, Callable] = {}
        self._discover_tools()
        logger.info(f"ComputerUse initialized with {len(self._tools)} tools")

    def _discover_tools(self) -> None:
        """Auto-discover all tool functions in this directory."""
        current_dir = os.path.dirname(os.path.abspath(__file__))

        for filename in sorted(os.listdir(current_dir)):
            if not filename.endswith('.py') or filename == '__init__.py':
                continue
            if filename.startswith('_'):
                continue  # helper modules, not tools

            tool_name = filename[:-3]
            module_path = f"tools.computer_use.{tool_name}"

            try:
                module = importlib.import_module(module_path)
                func = getattr(module, tool_name, None)
                if func and callable(func):
                    self._tools[tool_name] = func
                    logger.debug(f"Discovered computer_use tool: {tool_name}")
            except Exception as e:
                logger.error(f"Failed to load computer_use tool '{tool_name}': {e}")

    def get_all_tools(self) -> Dict[str, Callable]:
        """Return all discovered tools."""
        return self._tools.copy()

    def get_tool_count(self) -> int:
        return len(self._tools)
