"""Ensure connected function - helper to verify connection before executing commands"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "ensure_connected",
        "description": "Internal helper: verifies Chrome connection is active. Auto-connects if disconnected. Returns True/False.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def ensure_connected() -> bool:
    """Ensure a usable browser page exists before running a command.

    Calls connect() (which may auto-start Chrome and create a new tab) whenever the
    shared WebInteract instance is missing, disconnected, or has no live page. Returns
    True only if a page object is available afterwards.

    2026-09-14 reliability fix: the old code did 'from .connect import connect' inside
    this function -- on the CLIENT that same-package import is NEUTRALIZED by the source
    transform (it becomes a bare pass), and connect.py is not in ensure_connected's
    shared_deps, so 'do_connect' was an unbound name: every auto-reconnect raised
    NameError and tools failed with 'No page available'. The reconnect now goes through
    the anchor-registered function (shared_instance.do_connect_fallback), which works no
    matter which exec'd copy of this file is running.
    """
    from .shared_instance import get_web_interact, do_connect_fallback

    wi = get_web_interact()
    if not wi or not wi._connected or not wi.page:
        # Trigger a real (re)connect through the anchor-registered connect tool.
        result = await do_connect_fallback()
        logger.debug(f"ensure_connected: reconnect attempt -> {result[:120]}")

        wi = get_web_interact()
        if not wi or not wi._connected:
            return False
        if wi.page is None and wi.context:
            try:
                wi.page = await wi.context.new_page()
            except Exception as e:
                logger.warning(f"ensure_connected: could not create page after reconnect: {e}")
    return wi.page is not None
