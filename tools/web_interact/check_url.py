"""Check URL function - quickly checks if a URL is valid/accessible with SSRF protection"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "check_url",
        "description": "Check whether a URL is reachable in the connected browser. Navigates to it with minimal wait (commit), reports the HTTP status, then restores your previous page. SSRF-protected: private/internal addresses are blocked. Auto-adds https:// if missing.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to check. Auto-adds https:// if missing."},
                "timeout": {"type": "integer", "description": "Max wait time in ms", "default": 5000}
            },
            "required": ["url"]
        }
    }
}

import logging

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def check_url(url: str, timeout: int = 5000) -> str:
    """Check whether a URL is reachable in the connected browser.

    Navigates to the URL with wait_until='commit' (no full-page render wait),
    reports the HTTP status, then goes back to restore your previous page.
    SSRF-protected: private/internal addresses are blocked before navigation.
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
        # Normalize URL scheme (2026-07-15): file:// stays INTACT for the SSRF gate's
        # consent classification; https:// auto-added only to bare hostnames.
        if not url.startswith(('http://', 'https://', 'file://')):
            url = 'https://' + url

        # --- SSRF Protection (fail-closed, no fallbacks — 2026-08-28 hardening) ---
        try:
            from tools.ssrf_guard import ensure_url_not_ssrff
            ensure_url_not_ssrff(url)
        except ImportError as e:
            logger.error(f"SSRF defense module unavailable in check_url, refusing to proceed (no fallback): {e!r}")
            return "Error: SSRF validation unavailable — request refused."
        except Exception as e:  # includes SSRFError and any unexpected failure -> hard stop
            logger.error(f"SSRF validation failed in check_url, refusing to proceed (no fallback): {e!r}")
            return f"Error: Access blocked for security. ({str(e)[:200]})"
        # Save current URL to restore later
        current_url = wi.page.url

        # Navigate with minimal waiting - just get response
        response = await wi.page.goto(url, wait_until="commit", timeout=timeout)

        status = response.status if response else 0

        if 200 <= status < 400:
            result = f"✅ URL is valid: {url} (HTTP {status})"
        else:
            result = f"❌ URL returned HTTP {status}: {url}"

        # Go back to original page
        try:
            await wi.page.go_back(wait_until="commit")
        except Exception:
            logger.debug("Non-critical exception caught at tools/web_interact/check_url.py")
            await wi.page.goto(current_url, wait_until="commit", timeout=10000)

        return result

    except Exception as e:
        error_msg = str(e).lower()
        if "timeout" in error_msg:
            return f"⏱️ Timeout - URL may be slow or unreachable: {url}"
        elif "404" in error_msg or "not found" in error_msg:
            return f"❌ URL not found (404): {url}"
        else:
            return f"❌ Invalid or unreachable URL: {url} (Error: {str(e)[:100]})"
