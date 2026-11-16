"""Shared logger for the direct_ws package.

Keeps the logger name used by ws_client_handler.py, so log output is identical
whether the code runs from the package or through the shim.
"""

import logging

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")
