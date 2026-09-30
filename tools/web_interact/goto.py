"""Goto function - navigates to a URL with SSRF protection"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "goto",
        "description": "Navigate to a specific URL. Use wait_until='commit' for fast loads or 'networkidle' for full rendering. Auto-adds https:// if missing.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Target URL to navigate to"},
                "wait_until": {"type": "string", "description": "When to consider navigation successful", "default": "commit"},
                "timeout": {"type": "integer", "description": "Max wait time in ms", "default": 60000}
            },
            "required": ["url"]
        }
    }
}

import logging
import asyncio

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def goto(url: str, wait_until: str = "commit", timeout: int = 60000) -> str:
    """Navigate to a URL with SSRF protection."""
    from .ensure_connected import ensure_connected
    from .shared_instance import get_web_interact, ensure_page

    if not await ensure_connected():
        return "❌ Not connected to Chrome."

    await ensure_page()  # self-heal: re-acquire or create a live page (2026-09-10 fix)
    wi = get_web_interact()
    if not wi or wi.page is None:
        return "❌ No page available."

    # Normalize URL scheme before SSRF check
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    # --- SSRF Protection (fail-closed, no fallbacks — 2026-08-28 hardening) ---
    try:
        from tools.ssrf_guard import ensure_url_not_ssrff
        ensure_url_not_ssrff(url)
    except ImportError as e:
        logger.error(f"SSRF defense module unavailable in goto, refusing to proceed (no fallback): {e!r}")
        return "Error: SSRF validation unavailable — request refused."
    except Exception as e:  # includes SSRFError and any unexpected failure -> hard stop
        logger.error(f"SSRF validation failed in goto, refusing to proceed (no fallback): {e!r}")
        return f"Error: Access blocked for security. ({str(e)[:200]})"
    try:
        await wi.page.goto(url, wait_until=wait_until, timeout=timeout)

        try:
            await wi.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            logger.debug("Timeout handled at tools/web_interact/goto.py")

        await asyncio.sleep(0.5)
        title = await wi.page.title()
        return f"✅ Navigated to: {url}\n📄 Title: {title}\n🔗 URL: {wi.page.url}"
    except Exception as e:
        return f"❌ Navigation failed: {str(e)}"
