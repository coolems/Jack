"""Disconnect function - closes connection to Chrome browser"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "disconnect",
        "description": "Close the connection to Chrome and free Playwright resources. Use when web automation is complete.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def disconnect() -> str:
    """Close the connection to Chrome.

    Stops the Playwright driver AND clears wi.playwright: connect reuses the driver, so a
    stale reference to a stopped object would be handed to connect_over_cdp and raise
    "Target page, context or browser has been closed". The launch heuristic reads the word
    'connect' in that message as "nothing on the port", launches a SECOND debug Chrome
    window and then polls a port the first launch already bound. Clearing the reference
    makes the next connect start a fresh driver (~1.2s) and never spawn a second browser.
    """
    from .shared_instance import get_web_interact

    try:
        wi = get_web_interact()
        if wi and wi.playwright:
            await wi.playwright.stop()
        if wi:
            wi.playwright = None  # a stopped driver must never be reused
            wi._connected = False
            wi.browser = None
            wi.context = None
            wi.page = None
        return "✅ Disconnected from Chrome browser."
    except Exception as e:
        return f"❌ Error disconnecting: {str(e)}"
