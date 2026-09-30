"""Scroll function - scrolls the page"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "scroll",
        "description": "Scroll the current browser page up, down, to top, or to bottom. Use pixels or preset directions.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "description": "Scroll direction: up, down, top, bottom", "default": "down"},
                "amount": {"type": "integer", "description": "Pixels to scroll (used for up/down)", "default": 500}
            },
            "required": []
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def scroll(direction: str = "down", amount: int = 500) -> str:
    """Scroll the page."""
    from .ensure_connected import ensure_connected
    from .shared_instance import get_web_interact
    
    if not await ensure_connected():
        return "❌ Not connected to Chrome."

    from .shared_instance import ensure_page
    await ensure_page()  # self-heal: re-acquire or create a live page (2026-09-14 alias fix: CLIENT transform neutralizes same-package imports; aliases are never injected into exec globals -> NameError)
    wi = get_web_interact()
    if not wi or wi.page is None:
        return "❌ No page available."
    try:
        if direction == "down":
            await wi.page.evaluate(f"window.scrollBy(0, {amount})")
        elif direction == "up":
            await wi.page.evaluate(f"window.scrollBy(0, -{amount})")
        elif direction == "top":
            await wi.page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            await wi.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            return f"❌ Unknown direction: {direction}"
        return f"✅ Scrolled {direction}"
    except Exception as e:
        return f"❌ Scroll failed: {str(e)}"
