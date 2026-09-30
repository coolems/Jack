"""Reload function - reloads the current page"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "reload",
        "description": "Reload/refresh the current browser page. Useful when content is stale or scripts failed to load.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}

import logging
import asyncio

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def reload() -> str:
    """Reload the current page."""
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
        await wi.page.reload(wait_until="commit")
        await asyncio.sleep(0.5)
        title = await wi.page.title()
        return f"✅ Page reloaded: {title}"
    except Exception as e:
        return f"❌ Reload failed: {str(e)}"
