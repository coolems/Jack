"""Shared logger for the direct_ws package.

Same logger name as the old monolithic ws_client_handler.py so log output is
indistinguishable after the 2026-09-07 split.
"""

import logging

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")
