"""Fetch URL function - extracts content from a webpage with SSRF protection."""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "Fetch and extract readable text content from a SINGLE webpage URL. Returns page title and cleaned article/text content (truncated to 8000 chars). Strips scripts, styles, and navigation. Use ONLY when you already have a direct URL to read. Do NOT use for navigating, clicking, or JS-heavy sites (use web automation/browser instead). Always verify URL starts with http:// or https://.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to fetch. Must be a direct link to a webpage. Example: 'https://example.com/article' or 'https://news.site/story.html'"
                }
            },
            "required": ["url"]
        }
    }
}

import asyncio
import logging
import random
import re
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

# Single source of truth for SSRF validation + transport-level pinning (2026-09).
# ensure_url_not_ssrff: fast logged pre-check on the initial URL.
# safe_session: every connection -- including ALL redirect hops and IP-literal targets --
# is resolved once, validated against private/reserved ranges, and pinned to those exact
# IPs inside the connector. No global state, no monkeypatching, asyncio-only.
from tools.ssrf_guard import ensure_url_not_ssrff, safe_session, SSRFError

logger = logging.getLogger("COOLEMS.Tools.Web")


async def fetch_url(url: str) -> str:
    """Fetch and extract content from a URL with SSRF protection (asyncio-native)."""

    # --- SSRF Check on initial URL (shared module, strict + fail-closed) ---
    try:
        ensure_url_not_ssrff(url)
    except Exception as e:  # SSRFError or any validation failure -> hard stop, no fallback
        logger.error(f"SSRF blocked by fetch_url (no fallback): {e!r} — url={url}")
        return "Error: Access blocked for security."

    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]
    headers = {
        'User-Agent': random.choice(user_agents),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    try:
        # SSRF pinning happens INSIDE safe_session's connector (2026-09 rewrite):
        # each hostname is resolved once, validated, and pinned for the session --
        # redirects are covered by construction because every new connection goes
        # through PinnedConnector._resolve_host. No hooks, no global state.
        async with safe_session(timeout=20) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as response:
                if response.status >= 400:
                    return f"HTTP Error: {response.status} for url: {url}"
                content = await response.read()
                final_url = str(response.url)

        soup = BeautifulSoup(content, 'html.parser')

        # Remove unwanted elements
        for script in soup(["script", "style", "nav", "header", "footer", "aside", "iframe", "noscript"]):
            script.decompose()

        title = soup.title.string if soup.title else "No title"
        result = f"Title: {title}\nURL: {final_url}\n\n"

        # Look for articles
        articles = soup.find_all('article')
        if len(articles) > 1:
            result += f"=== NEWS ARTICLES ({len(articles)} found) ===\n\n"
            for i, article in enumerate(articles[:20]):
                h = article.find(['h1', 'h2', 'h3', 'h4'])
                if h:
                    headline = h.get_text(strip=True)
                    if headline and len(headline) > 5:
                        link = article.find('a', href=True)
                        href = link['href'] if link else ''
                        p = article.find('p')
                        summary = p.get_text(strip=True)[:150] if p else ''
                        result += f"{i+1}. {headline}\n"
                        if summary and len(summary) > 20:
                            result += f"   {summary}...\n"
                        if href:
                            if href.startswith('/'):
                                href = urljoin(final_url, href)
                            result += f"   Link: {href}\n"
                        result += "\n"
            if len(articles) > 20:
                result += f"[...and {len(articles) - 20} more articles]\n"
        else:
            # Extract main content
            main_content = soup.find('article') or soup.find('main') or soup.find('div', class_=re.compile(r'(content|article|main|post)', re.I)) or soup.body
            if main_content:
                text = main_content.get_text(separator='\n', strip=True)
            else:
                text = soup.get_text(separator='\n', strip=True)

            lines = [l.strip() for l in text.splitlines() if l.strip() and len(l.strip()) > 3]
            text = '\n'.join(lines)
            result += "=== PAGE CONTENT ===\n"
            result += text[:8000]
            if len(text) > 8000:
                result += "\n\n[Content truncated...]"

        return result

    except SSRFError as e:
        # Pinned-IP contract violation (redirect to private target, DNS rebinding, ...)
        # -> hard stop, no fallback.
        logger.error(f"SSRF protection triggered in fetch_url: {e}")
        return "Error: Access blocked for security."
    except asyncio.TimeoutError as e:
        return f"Timeout: The website took too long to respond. URL: {url}"
    except aiohttp.ClientError as e:
        msg = str(e) or type(e).__name__
        if 'resolve' in msg.lower() or 'dns' in msg.lower():
            return f"Fetch error: could not resolve host for {url}"
        return f"Fetch error: {msg}"
    except Exception as e:
        return f"Fetch error: {str(e)}"
