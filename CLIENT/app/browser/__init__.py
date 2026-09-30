"""app.browser - LAZY re-export of WebInteract for CLIENT compatibility.

The actual implementation lives ONLY on the SERVER (tools/web_interact/) and is
delivered to this client over WebSocket with tools_response, where it is
registered in sys.modules as 'tools.web_interact'. There is intentionally NO
local copy of web_interact source inside the CLIENT distribution.

Because startup imports run BEFORE the first WS fetch completes, WebInteract is
resolved LAZILY: attribute access (e.g. WebInteract.cleanup) defers to whenever
the delivered module exists in sys.modules. If it never arrives (SERVER down at
startup), a no-op stand-in keeps the client bootable -- browser tools simply
report 'not connected' until the server delivers them.
"""

import logging
import sys
from types import ModuleType

logger = logging.getLogger("COOLEMS.AppBrowser")


def _resolve_web_interact():
    """Return the SERVER-delivered WebInteract class, or a no-op stand-in."""
    mod = sys.modules.get('tools.web_interact')
    if mod is not None and hasattr(mod, 'WebInteract'):
        return mod.WebInteract

    logger.debug("SERVER web_interact module not delivered yet - using no-op stand-in")

    class _NoOpWebInteract:
        """Stand-in used ONLY until the SERVER delivers tools.web_interact."""

        @staticmethod
        def cleanup() -> None:
            return None

    return _NoOpWebInteract


class _LazyBrowserModule(ModuleType):
    """Module proxy that resolves WebInteract on first attribute access.

    This keeps `from app.browser import WebInteract` working at startup while
    guaranteeing the SERVER-delivered class is what gets used (never a local copy).
    """

    def __getattr__(self, name: str):
        if name == "WebInteract":
            return _resolve_web_interact()
        raise AttributeError(f"module 'app.browser' has no attribute {name!r}")


# Replace this module in sys.modules with the lazy proxy (keeps import identity).
import sys as _sys
_sys.modules[__name__] = _LazyBrowserModule(__name__)

__all__ = ["WebInteract"]
