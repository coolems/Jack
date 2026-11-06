"""HTTP adapters for web_search - one provider per function, uniform contract.

Every provider returns a dict:
    {"ok": bool, "items": [{"title","url","snippet","date"}], "note": str}

Items are RAW (no normalisation): web_search.py owns URL normalisation, snippet
cleaning and dedup via _search_core. Keeping the adapters free of sibling imports
is deliberate - the delivery pipeline injects shared_deps into the TOOL's globals,
not into another dep's globals, so a dep must never import a dep.

Honest-failure contract (2026-11-06): ok=True ONLY when at least one usable result
was parsed. HTTP >= 400, a bot wall, or zero parsed items => ok=False with a note.
The old tool accepted "DuckDuckGo returned status 202 (blocked)" as a result, which
made a failed search look successful while returning nothing.

No dangerous modules (requests + stdlib only) - no sandbox policy rows needed.
"""

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote as url_quote

import requests

# Browser UA for HTML/RSS endpoints; bot UA for API endpoints (Wikimedia rejects
# browser-looking UAs with 403 on its search API).
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
BOT_UA = "COOLEMS/1.0 (web_search tool)"

_HTML_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_RSS_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_API_HEADERS = {"User-Agent": BOT_UA}


def _fail(note: str) -> dict:
    return {"ok": False, "items": [], "note": note}


def _ok(items: list, note: str = "") -> dict:
    usable = [i for i in items if isinstance(i.get("url"), str) and i.get("url").startswith("http")]
    if not usable:
        return _fail(note or "no usable results")
    return {"ok": True, "items": usable, "note": note}


def _item(title, url, snippet, date=None) -> dict:
    return {"title": title or "", "url": url or "", "snippet": snippet or "", "date": date or ""}


def _get(url, params=None, headers=None, timeout: float = 12.0):
    return requests.get(url, params=params, headers=headers or _API_HEADERS, timeout=timeout)


# ---------------------------------------------------------------------------
# General web engines
# ---------------------------------------------------------------------------

def bing_rss(query: str, limit: int, settings: dict) -> dict:
    """Bing RSS endpoint - structured XML, direct destination URLs, ~0.7 s.

    Deliberately NO mkt/cc parameter: Bing's market targeting returns 0 items for
    many queries with mkt=en-US and rewrites the query with cc=US (measured
    2026-11-06). Language twins are collapsed by _search_core.dedup_key instead.
    """
    timeout = settings.get("timeout") or 12
    try:
        response = _get("https://www.bing.com/search",
                        {"q": query, "format": "rss"}, _RSS_HEADERS, timeout)
    except Exception as exc:
        return _fail(f"bing_rss: {type(exc).__name__}: {str(exc)[:60]}")
    if response.status_code != 200:
        return _fail(f"bing_rss: HTTP {response.status_code}")
    try:
        root = ET.fromstring(response.text)
    except Exception:
        return _fail("bing_rss: response was not valid RSS")
    items = []
    for entry in root.findall(".//item"):
        url = entry.findtext("link") or ""
        if not url.startswith("http"):
            continue
        items.append(_item(entry.findtext("title"), url,
                           entry.findtext("description"), entry.findtext("pubDate")))
        if len(items) >= limit:
            break
    if not items:
        return _fail("bing_rss: 0 items")
    return _ok(items)


def bing_html(query: str, limit: int, settings: dict) -> dict:
    """Bing HTML fallback - used when the RSS endpoint returns no items."""
    timeout = settings.get("timeout") or 12
    try:
        response = _get("https://www.bing.com/search", {"q": query}, _HTML_HEADERS, timeout)
    except Exception as exc:
        return _fail(f"bing: {type(exc).__name__}: {str(exc)[:60]}")
    if response.status_code != 200:
        return _fail(f"bing: HTTP {response.status_code}")
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _fail("bing: beautifulsoup4 not installed")
    soup = BeautifulSoup(response.text, "html.parser")
    items = []
    for block in soup.select(".b_algo"):
        anchor = block.select_one("h2 a") or block.select_one("a")
        if not anchor:
            continue
        href = anchor.get("href", "")
        if not href.startswith("http"):
            continue
        caption = block.select_one(".b_caption p") or block.select_one(".b_desc")
        items.append(_item(anchor.get_text(strip=True), href,
                           caption.get_text(strip=True) if caption else ""))
        if len(items) >= limit:
            break
    if not items:
        return _fail("bing: 0 results (bot wall or empty page)")
    return _ok(items)


def ddg_html(query: str, limit: int, settings: dict) -> dict:
    """DuckDuckGo HTML endpoint. A 200 with no result markers is a bot wall -> fail."""
    timeout = settings.get("timeout") or 12
    try:
        response = requests.get(f"https://html.duckduckgo.com/html/?q={url_quote(query)}",
                                headers=_HTML_HEADERS, timeout=timeout)
    except Exception as exc:
        return _fail(f"ddghtml: {type(exc).__name__}: {str(exc)[:60]}")
    if response.status_code != 200:
        return _fail(f"ddghtml: HTTP {response.status_code} (blocked)")
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _fail("ddghtml: beautifulsoup4 not installed")
    soup = BeautifulSoup(response.text, "html.parser")
    items = []
    for block in soup.select(".result"):
        link = block.select_one(".result__a")
        if not link:
            continue
        snippet = block.select_one(".result__snippet")
        items.append(_item(link.get_text(strip=True), link.get("href", ""),
                           snippet.get_text(strip=True) if snippet else ""))
        if len(items) >= limit:
            break
    if not items:
        return _fail("ddghtml: 0 results (bot wall)")
    return _ok(items)


def ddgs_lib(query: str, limit: int, settings: dict) -> dict:
    """DuckDuckGo via the ddgs package (pinned in requirements.txt; absent in some venvs).

    ddgs is a metasearcher: it queries several backends and raises TimeoutException if
    any of them stalls (Yahoo's endpoint is slow here), so its own timeout is passed in
    and the exception is reported as a provider failure rather than retried blindly.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return _fail("ddgs: package not installed")
    timeout = settings.get("timeout") or 8
    try:
        with DDGS(timeout=int(timeout)) as ddgs:
            rows = list(ddgs.text(query, max_results=limit))
    except Exception as exc:
        return _fail(f"ddgs: {type(exc).__name__}: {str(exc)[:60]}")
    items = [_item(r.get("title"), r.get("href") or r.get("url"),
                   r.get("body") or r.get("description") or "") for r in rows]
    if not items:
        return _fail("ddgs: 0 results")
    return _ok(items)


def searxng(query: str, limit: int, settings: dict) -> dict:
    """SearXNG instances from config. Down instances are reported, not retried blindly."""
    instances = settings.get("instances") or []
    if not instances:
        return _fail("searxng: no instances configured")
    timeout = settings.get("timeout") or 12
    notes = []
    for instance in instances[:2]:
        try:
            response = _get(f"{instance.rstrip('/')}/search",
                            {"q": query, "format": "json"}, _HTML_HEADERS, timeout)
        except Exception as exc:
            notes.append(f"{instance}: {type(exc).__name__}")
            continue
        if response.status_code != 200:
            notes.append(f"{instance}: HTTP {response.status_code}")
            continue
        try:
            rows = response.json().get("results", [])
        except Exception:
            notes.append(f"{instance}: not JSON")
            continue
        items = [_item(r.get("title"), r.get("url"), r.get("content")) for r in rows]
        if items:
            return _ok(items[:limit])
        notes.append(f"{instance}: 0 results")
    return _fail("searxng: " + " | ".join(notes))


# ---------------------------------------------------------------------------
# Specialist providers (intent-routed by _search_core.route_providers)
# ---------------------------------------------------------------------------

def wikipedia_rest(query: str, limit: int, settings: dict) -> dict:
    """Wikipedia REST search - encyclopaedic/definition queries only."""
    timeout = settings.get("timeout") or 12
    try:
        response = _get("https://en.wikipedia.org/w/rest.php/v1/search/page",
                        {"q": query, "limit": min(limit, 10)}, _API_HEADERS, timeout)
    except Exception as exc:
        return _fail(f"wikipedia: {type(exc).__name__}: {str(exc)[:60]}")
    if response.status_code != 200:
        return _fail(f"wikipedia: HTTP {response.status_code}")
    try:
        pages = response.json().get("pages", [])
    except Exception:
        return _fail("wikipedia: bad JSON")
    items = []
    for page in pages[:limit]:
        title = page.get("title") or ""
        if not title:
            continue
        items.append(_item(title,
                           "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
                           page.get("description") or ""))
    if not items:
        return _fail("wikipedia: 0 pages")
    return _ok(items)


def github_repos(query: str, limit: int, settings: dict) -> dict:
    """GitHub repository search - code/library/package intent."""
    timeout = settings.get("timeout") or 12
    try:
        response = _get("https://api.github.com/search/repositories",
                        {"q": query, "per_page": min(limit, 20)},
                        {**_API_HEADERS, "Accept": "application/vnd.github+json"}, timeout)
    except Exception as exc:
        return _fail(f"github: {type(exc).__name__}: {str(exc)[:60]}")
    if response.status_code != 200:
        return _fail(f"github: HTTP {response.status_code}")
    try:
        rows = response.json().get("items", [])
    except Exception:
        return _fail("github: bad JSON")
    items = []
    for repo in rows[:limit]:
        note = repo.get("description") or ""
        stars = repo.get("stargazers_count")
        if stars:
            note = f"{stars} stars - {note}"
        items.append(_item(repo.get("full_name") or repo.get("name"),
                           repo.get("html_url"), note, repo.get("updated_at")))
    if not items:
        return _fail("github: 0 repositories")
    return _ok(items)


def hackernews(query: str, limit: int, settings: dict) -> dict:
    """HN Algolia - discussion/opinion intent."""
    timeout = settings.get("timeout") or 12
    try:
        response = _get("https://hn.algolia.com/api/v1/search",
                        {"query": query, "hitsPerPage": min(limit, 20)}, _API_HEADERS, timeout)
    except Exception as exc:
        return _fail(f"hackernews: {type(exc).__name__}: {str(exc)[:60]}")
    if response.status_code != 200:
        return _fail(f"hackernews: HTTP {response.status_code}")
    try:
        hits = response.json().get("hits", [])
    except Exception:
        return _fail("hackernews: bad JSON")
    items = []
    for hit in hits[:limit]:
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        items.append(_item(hit.get("title") or "(HN story)", url,
                           f"{hit.get('points') or 0} points on Hacker News",
                           hit.get("created_at")))
    if not items:
        return _fail("hackernews: 0 stories")
    return _ok(items)


def arxiv(query: str, limit: int, settings: dict) -> dict:
    """arXiv Atom feed - paper/preprint intent."""
    timeout = settings.get("timeout") or 15
    terms = [t for t in re.split(r"\s+", query) if t][:6]
    # Literal spaces, not '+': requests encodes a space as '+' (arXiv's boolean
    # operator), while a literal '+' is percent-encoded and arXiv then matches
    # nothing (measured 2026-11-06: 'all:x+OR+all:y' -> 0 entries, 'all:x OR all:y' -> 5).
    search_query = " OR ".join(f"all:{t}" for t in terms) or f"all:{query}"
    try:
        response = _get("https://export.arxiv.org/api/query",
                        {"search_query": search_query, "max_results": min(limit, 20)},
                        _API_HEADERS, timeout)
    except Exception as exc:
        return _fail(f"arxiv: {type(exc).__name__}: {str(exc)[:60]}")
    if response.status_code != 200:
        return _fail(f"arxiv: HTTP {response.status_code}")
    namespaces = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(response.text)
    except Exception:
        return _fail("arxiv: bad Atom feed")
    items = []
    for entry in root.findall("atom:entry", namespaces):
        title = (entry.findtext("atom:title", "", namespaces) or "").strip()
        link = (entry.findtext("atom:id", "", namespaces) or "").strip()
        if not title or not link.startswith("http"):
            continue
        # arXiv keeps withdrawn entries in the feed; they carry no content.
        if title.lower().startswith("this paper has been withdrawn"):
            continue
        items.append(_item(title, link,
                           (entry.findtext("atom:summary", "", namespaces) or "").strip(),
                           entry.findtext("atom:updated", "", namespaces)))
        if len(items) >= limit:
            break
    if not items:
        return _fail("arxiv: 0 papers")
    return _ok(items)


# id -> adapter. ids must match [a-z0-9_]{1,40} (CLIENT config validator).
PROVIDERS = {
    "bing_rss": bing_rss,
    "bing": bing_html,
    "ddghtml": ddg_html,
    "ddgs": ddgs_lib,
    "searxng": searxng,
    "wikipedia": wikipedia_rest,
    "github": github_repos,
    "hackernews": hackernews,
    "arxiv": arxiv,
}
