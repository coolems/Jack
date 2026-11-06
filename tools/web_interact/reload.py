"""Reload function - reloads the current page"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "reload",
        "description": "Reload/refresh the current browser page. Returns as soon as the reload is committed; pass wait_until='networkidle' when you need deferred scripts to finish.",
        "parameters": {
            "type": "object",
            "properties": {
                "wait_until": {"type": "string", "description": "commit (default), domcontentloaded, load, or networkidle", "default": "commit"}
            },
            "required": []
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def reload(wait_until: str = "commit") -> str:
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
        await wi.page.reload(wait_until=wait_until)

        # No fixed pause here (2026-11-06): the old unconditional sleep(0.5) added
        # 0.5 s to every reload. wait_until is honoured by Playwright itself.
        title = await wi.page.title()
        if not title and wait_until == "commit":
            # Reload committed but the DOM may not be parsed yet: bounded window
            # only when the title is actually missing.
            try:
                await wi.page.wait_for_load_state("domcontentloaded", timeout=400)
                title = await wi.page.title()
            except Exception:
                logger.debug("Title not available yet after reload commit at tools/web_interact/reload.py")
        return f"✅ Page reloaded: {title}"
    except Exception as e:
        return f"❌ Reload failed: {str(e)}"
