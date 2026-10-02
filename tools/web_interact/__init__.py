"""
Web Interact - Dynamic Entry Point

This module provides browser automation capabilities.
NO Ollama dependencies - pure Playwright/CDP automation.

ASYNCIO-NATIVE (2026-08-29 threadless refactor):
  The old design kept a dedicated OS thread running its own event loop
  (_global_loop) and bridged every tool call into it via
  asyncio.run_coroutine_threadsafe + future.result(timeout). That second loop
  was unnecessary: the browser tools are async functions that run on the MAIN
  server/client event loop (delivered tools are awaited by RemoteToolOrchestrator,
  local tools are now wrapped as plain coroutines below). No thread exists in this
  module anymore - everything runs on the caller's running loop.

  Consequences:
    * get_all_tools() returns ASYNC callables (await them).
    * connect_sync()/disconnect_sync()/_run_async() are GONE.
    * cleanup() remains as a harmless no-op for API compatibility
      (code_client.py registers WebInteract.cleanup via atexit BEFORE the SERVER
      delivers this module, so the name must keep existing).
"""

import inspect
import logging
import os
import importlib
from typing import Dict, Callable

logger = logging.getLogger("COOLEMS.Tools.WebInteract")

def _resolve_cdp_url() -> str:
    """Build the CDP endpoint URL from CHROME_CDP_PORT - NO hardcoded port, NO fallback.

    Resolution order (2026-10-02 no-fallback contract):
      1. Bare name ``CHROME_CDP_PORT`` in module globals -- on the CLIENT the SERVER
         ships this constant via tools_response 'config_constants' and the loader injects
         it into exec globals ('from config import ...' lines are neutralized there).
      2. ``from config import CHROME_CDP_PORT`` -- normal import on the SERVER, where
         config/config.py is the single source of truth.

    If NEITHER provides the constant this raises RuntimeError instead of silently using a
    hardcoded port - a missing value is a configuration error and must be fixed in
    config, not papered over at runtime. (127.0.0.1 avoids IPv6 ::1 ECONNREFUSED.)
    """
    try:
        port = CHROME_CDP_PORT  # noqa: F821 - injected exec global on the CLIENT (path 1)
    except NameError:
        pass
    else:
        return f"http://127.0.0.1:{port}"
    try:
        from config import CHROME_CDP_PORT as _port  # path 2 - SERVER; neutralized to 'pass' in delivered copies
    except ImportError as e:
        raise RuntimeError(
            "WebInteract cannot determine the Chrome CDP port: the CHROME_CDP_PORT constant is "
            "not available (SERVER did not ship it via config_constants and 'config' is not "
            "importable). Set CHROME_CDP_PORT in config/config.py - no fallback port is used."
        ) from e
    try:
        return f"http://127.0.0.1:{_port}"  # unbound when the import above was neutralized -> loud error below
    except NameError as e:
        raise RuntimeError(
            "WebInteract cannot determine the Chrome CDP port: no injected CHROME_CDP_PORT constant "
            "and 'from config import ...' is unavailable in this execution context. Set "
            "CHROME_CDP_PORT in config/config.py - no fallback port is used."
        ) from e



class WebInteract:
    """
    Browser automation tool that connects to your existing Chrome browser
    using Chrome DevTools Protocol (CDP).

    Prerequisite: Chrome must be started with:
    chrome --remote-debugging-port=<CHROME_CDP_PORT from config> --user-data-dir=/tmp/chrome-debug
    """

    def __init__(self, cdp_url: str = None):
        if cdp_url is None:
            cdp_url = _resolve_cdp_url()
        self.cdp_url = cdp_url
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._connected = False
        self._tools: Dict[str, Callable] = {}

        # Register this instance as the shared singleton for remote tools
        try:
            from .shared_instance import set_web_interact
            set_web_interact(self)
        except Exception:
            pass

        self._discover_tools()
        logger.info(f"WebInteract initialized with {len(self._tools)} tools")

    def _discover_tools(self) -> None:
        """Auto-discover all tool functions in this directory.

        NOTE (remote execution): when this module is delivered to a CLIENT via
        shared_sources, __file__ is '<remote_web_interact>' and there is no local
        directory to scan -- the individual web tools are compiled separately by
        RemoteToolOrchestrator from their own tool_code requests. Discovery only
        applies on the SERVER where the real files exist on disk.

        The discovered callables are ASYNC: each wraps the standalone async tool
        function directly (no thread bridge, no second event loop). Callers must
        await them; they run on whatever loop is currently running.
        """
        if getattr(__file__, 'startswith', lambda _p: False)('<remote_'):
            logger.debug("WebInteract running in remote (delivered) mode - skipping filesystem tool discovery")
            return

        current_dir = os.path.dirname(os.path.abspath(__file__))

        for filename in sorted(os.listdir(current_dir)):
            if not filename.endswith('.py'):
                continue
            if filename == '__init__.py':
                continue
            if filename == 'shared_instance.py':
                continue

            tool_name = filename[:-3]
            module_path = f"tools.web_interact.{tool_name}"

            try:
                module = importlib.import_module(module_path)
                async_func = getattr(module, tool_name, None)

                if async_func and callable(async_func):
                    # The standalone functions use shared_instance.get_web_interact(),
                    # so no instance binding is needed. Wrap them as plain coroutines:
                    # they are awaited on the caller's running loop (main loop).
                    def make_bound_wrapper(func, name):
                        if 'self' in list(__import__('inspect').signature(func).parameters.keys()):
                            # Old-style function with self - pass instance
                            async def wrapper(*args, **kwargs):
                                return await func(self, *args, **kwargs)
                        else:
                            # New standalone async function - no self needed
                            async def wrapper(*args, **kwargs):
                                return await func(*args, **kwargs)
                        wrapper.__name__ = name
                        return wrapper

                    self._tools[tool_name] = make_bound_wrapper(async_func, tool_name)
                    logger.debug(f"Discovered web_interact tool: {tool_name}")

            except Exception as e:
                logger.error(f"Failed to load web_interact tool '{tool_name}': {e}")

    # Core connection methods (these are not auto-discovered)
    async def connect(self) -> str:
        from .connect import connect as do_connect
        return await do_connect()

    async def disconnect(self) -> str:
        from .disconnect import disconnect as do_disconnect
        return await do_disconnect()

    async def ensure_connected(self) -> bool:
        from .ensure_connected import ensure_connected as do_ensure
        return await do_ensure()

    def get_all_tools(self) -> Dict[str, Callable]:
        """Return all discovered tools (ASYNC callables - await them)."""
        return self._tools.copy()

    def get_tool_count(self) -> int:
        return len(self._tools)

    def __getattr__(self, name: str) -> Callable:
        if name in self._tools:
            return self._tools[name]
        raise AttributeError(f"WebInteract has no tool named '{name}'")

    @staticmethod
    def cleanup():
        """No-op kept for API compatibility.

        The old version stopped a dedicated event-loop thread; that thread no
        longer exists (2026-08-29 threadless refactor). code_client.py registers
        this via atexit at import time -- before the SERVER delivers this module
        -- so the attribute must keep existing and stay side-effect free.
        """
        return None
