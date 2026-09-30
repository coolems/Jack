"""Go back function - navigates back in history"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "go_back",
        "description": "Navigate back one step in browser history. Use to return to the previous page after viewing details.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def go_back() -> str:
    """Navigate back in history."""
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
        await wi.page.go_back()
        return f"✅ Went back to: {wi.page.url}"
    except Exception as e:
        return f"❌ Failed to go back: {str(e)}"
