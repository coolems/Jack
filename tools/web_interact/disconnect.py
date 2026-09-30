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
    """Close the connection to Chrome."""
    from .shared_instance import get_web_interact
    
    try:
        wi = get_web_interact()
        if wi and wi.playwright:
            await wi.playwright.stop()
        if wi:
            wi._connected = False
            wi.browser = None
            wi.context = None
            wi.page = None
        return "✅ Disconnected from Chrome browser."
    except Exception as e:
        return f"❌ Error disconnecting: {str(e)}"
