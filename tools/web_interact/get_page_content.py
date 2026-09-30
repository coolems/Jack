"""Get page content function - returns HTML and visible text of current page"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "get_page_content",
        "description": "Extract visible text content and raw HTML from the current page. Returns URL, title, and cleaned text. Truncated for context limits.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}

import logging
import json

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def get_page_content() -> str:
    """Get the current page's HTML content and visible text."""
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
        html = await wi.page.content()
        visible_text = await wi.page.evaluate("() => document.body.innerText")
        title = await wi.page.title()
        url = wi.page.url
        
        if len(html) > 10000:
            html = html[:10000] + "... [truncated]"
        if len(visible_text) > 5000:
            visible_text = visible_text[:5000] + "... [truncated]"
        
        return json.dumps({
            "success": True,
            "url": url,
            "title": title,
            "html": html,
            "visible_text": visible_text
        }, indent=2)
    except Exception as e:
        return f"❌ Failed to get page content: {str(e)}"
