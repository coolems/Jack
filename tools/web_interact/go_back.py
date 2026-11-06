"""Go back function - navigates back in history"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "go_back",
        "description": "Navigate back one step in browser history. Use to return to the previous page after viewing details.",
        "parameters": {
            "type": "object",
            "properties": {
                "wait_until": {
                    "type": "string",
                    "description": "commit (default, returns as soon as the navigation is committed), domcontentloaded, load, or networkidle.",
                    "enum": ["commit", "domcontentloaded", "load", "networkidle"]
                },
                "timeout": {"type": "integer", "description": "Max wait time in ms (default 30000)."}
            },
            "required": []
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def go_back(wait_until: str = "commit", timeout: int = 30000) -> str:
    """Navigate back in history.

    2026-11-06 latency fix (measured on science.nasa.gov/gallery/hubbles-galaxies/):
    Playwright's go_back() defaults to wait_until="load", which waits for every image,
    script and iframe of the restored page. A 32-image gallery never finishes 'load'
    inside the 30 s default, so go_back() reported "Timeout 30000ms exceeded" even though
    the browser had already navigated to the gallery. commit returns as soon as the
    navigation is committed - the same default goto() uses - and the caller can still ask
    for a heavier state explicitly.
    """
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
        await wi.page.go_back(wait_until=wait_until, timeout=timeout)
        return f"✅ Went back to: {wi.page.url}"
    except Exception as e:
        return f"❌ Failed to go back: {str(e)}"
