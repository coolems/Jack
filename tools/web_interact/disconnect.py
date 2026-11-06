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

    2026-11-06 fix (the 'two Chrome windows opened' incident): this stopped the Playwright
    driver but left wi.playwright pointing at the STOPPED object. connect() reuses the driver
    (the 2026-11-06 latency fix), so the next connect() handed a dead driver to
    connect_over_cdp, which raised "Target page, context or browser has been closed". The
    launch heuristic matched the word 'connect' in that message, read it as "nothing on the
    port", and launched a SECOND debug Chrome window - then polled a port the FIRST launch had
    already bound, for 15s, and failed. Clearing the reference makes the next connect() start a
    fresh driver (~1.2s) and never spawn a second browser.
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
