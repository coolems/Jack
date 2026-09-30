"""Web search function - searches using multiple engines (config-driven).

Engine selection is driven by ``<CLIENT>/config/search_engines.json`` (single source
of truth, editable in the Settings UI). Each entry looks like:

    {"id": "wikipedia", "name": "Wikipedia", "enabled": true, "priority": 1,
     "settings": {"timeout": 15}}

- ``search_engines=None``        -> read <CLIENT>/config/search_engines.json live
- ``search_engines={"engines":[...]}`` or a bare list of engine dicts -> use as-is
- legacy bool dict ``{"wikipedia": true, ...}`` is still accepted (old clients)

To add an engine: add its entry to search_engines.json AND register an adapter in
_ENGINE_ADAPTERS below. Unknown ids are logged and skipped - never fatal.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the internet for current information, news, live data, or unknown facts. Returns up to 20 results with titles, URLs, and snippets. Use BEFORE fetching URLs or browsing. Do NOT use for local file operations or math.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Concise search query. Remove filler like 'please find' or 'tell me'. Example: 'qwen3.6 release date site:github.com'"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (1-20). Default: 10. Use 5 for quick checks, 15+ for deep research.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 20
                }
            },
            "required": ["query"]
        }
    }
}

import logging
import os
from bs4 import BeautifulSoup
import requests
from urllib.parse import quote as url_quote

from config import (
    WEB_SEARCH_DEFAULT_MAX_RESULTS,
    WEB_SEARCH_DDGS_DEFAULT_RESULTS,
    WEB_SEARCH_WIKIPEDIA_TIMEOUT,
    WEB_SEARCH_DDGS_TIMEOUT,
    WEB_SEARCH_BING_TIMEOUT,
    WEB_SEARCH_SEARXNG_TIMEOUT,
    WEB_SEARCH_RESULT_SNIPPET_CHARS,
    WEB_SEARCH_SEARXNG_INSTANCES,
)

logger = logging.getLogger("COOLEMS.Tools.Web")


# ---------------------------------------------------------------------------
# Config plumbing (search_engines.json is the single source of truth)
# ---------------------------------------------------------------------------

def _load_engine_config_from_json() -> list:
    """Read <CLIENT>/config/search_engines.json live. Returns [] on any problem.

    Path resolution (the CLIENT owns this file - no fallback engines are invented):
      1. <CWD>/config/search_engines.json       - normal CLIENT run (CWD = CLIENT dir)
      2. <local_ai>/CLIENT/config/...            - SERVER-side layout, where this module
         lives at local_ai/tools/web_tools (two levels below local_ai/)
    """
    try:
        import json as _json

        candidates = [os.path.join(os.getcwd(), "config", "search_engines.json")]
        try:
            module_dir = os.path.dirname(os.path.abspath(__file__))
            local_ai_root = os.path.dirname(os.path.dirname(module_dir))  # web_tools -> tools -> local_ai
            candidates.append(os.path.join(local_ai_root, "CLIENT", "config", "search_engines.json"))
        except Exception:
            pass

        path = None
        for candidate in candidates:
            try:
                if os.path.isfile(candidate):
                    path = candidate
                    break
            except Exception:
                continue
        if path is None:
            raise FileNotFoundError(
                "search_engines.json not found (looked in: " + ", ".join(candidates) + ")"
            )
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        engines = data.get("engines") if isinstance(data, dict) else None
        return [e for e in (engines or []) if isinstance(e, dict)]
    except Exception as e:
        logger.error(f"search_engines.json missing or invalid ({e}) - no search engines available. "
                     f"Fix the file (looked in: {', '.join(candidates)})")
        return []

def _normalize_search_engines(search_engines) -> list:
    """Normalize any accepted input form into an ordered [{id, enabled, settings}, ...] list.

    Accepted forms:
      None                       -> JSON file only (single source of truth, no built-in defaults)
      {"engines": [ ... ]}       -> the list inside
      [ {id, enabled, settings}, ... ] -> as-is
      {"wikipedia": true, ...}   -> legacy bool dict from old clients -> known adapter ids
    """
    if search_engines is None:
        # JSON file only - no built-in defaults. Missing/corrupt -> [] (error logged above).
        engines = _load_engine_config_from_json()
        return [e for e in engines if isinstance(e.get("enabled"), bool)]

    if isinstance(search_engines, dict):
        engines = search_engines.get("engines")
        if isinstance(engines, list):
            return [e for e in engines if isinstance(e, dict)]
        # Legacy bool-dict form: {"wikipedia": true, "ddgs": false, ...}
        known = set(_ENGINE_ADAPTERS.keys())
        result = []
        for i, (eid, enabled) in enumerate(search_engines.items(), 1):
            if eid not in known:
                continue
            result.append({"id": eid, "enabled": bool(enabled), "priority": i, "settings": {}})
        return result

    if isinstance(search_engines, list):
        return [e for e in search_engines if isinstance(e, dict)]

    logger.error(f"Unrecognized search_engines format ({type(search_engines).__name__}) - no engines used. "
                 f"Fix config/search_engines.json")
    return []


def _engine_settings(engine: dict) -> dict:
    """Per-engine settings from the JSON entry, filtered to what adapters understand.

    Keys absent here simply use the per-adapter default (the SERVER-shipped WEB_SEARCH_*
    constants) - this keeps optional keys optional; it is NOT an engine fallback.
    """
    s = engine.get("settings") or {}
    if not isinstance(s, dict):
        s = {}
    eid = engine.get("id", "")
    out = {"max_results": None}
    timeout = s.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        out["timeout"] = int(timeout)
    if eid == "ddgs":
        dr = s.get("default_results")
        if isinstance(dr, int) and not isinstance(dr, bool) and dr > 0:
            out["max_results"] = min(dr, WEB_SEARCH_DEFAULT_MAX_RESULTS)
    if eid == "searxng":
        instances = s.get("instances")
        if isinstance(instances, list) and instances and all(isinstance(x, str) for x in instances):
            out["instances"] = [x.rstrip("/") for x in instances]
    return out

# ---------------------------------------------------------------------------
# Engine adapters - one per supported engine. Each returns formatted result text
# (or None / an error-ish string the caller knows how to reject).
# ---------------------------------------------------------------------------

def _wikipedia_search(query: str, max_results: int = None, settings: dict = None) -> str:
    """Search Wikipedia API"""
    if max_results is None:
        max_results = WEB_SEARCH_DEFAULT_MAX_RESULTS
    timeout = (settings or {}).get("timeout") or WEB_SEARCH_WIKIPEDIA_TIMEOUT
    api_url = "https://en.wikipedia.org/w/api.php"
    params = {
        'action': 'query', 'list': 'search', 'srsearch': query,
        'format': 'json', 'srlimit': max_results, 'srprop': 'snippet|titlesnippet'
    }
    headers = {'User-Agent': 'COOLEMS/1.0 (AI Assistant; contact@example.com)'}
    try:
        response = requests.get(api_url, params=params, headers=headers, timeout=timeout)
        if response.status_code != 200:
            return None
        data = response.json()
        search_results = data.get('query', {}).get('search', [])
        if not search_results:
            return None
        formatted = []
        for i, result in enumerate(search_results[:max_results], 1):
            title = result.get('title', 'No title')
            snippet = result.get('snippet', 'No description')
            snippet = snippet.replace('<span class="searchmatch">', '**').replace('</span>', '**')
            page_url = f"https://en.wikipedia.org/wiki/{url_quote(title.replace(' ', '_'))}"
            formatted.append(f"{i}. {title}\n   URL: {page_url}\n   {snippet}")
        return f"Wikipedia Results:\n\n" + "\n\n".join(formatted)
    except Exception as e:
        logger.warning(f"Wikipedia search error: {e}")
        return None


def _ddgs_search(query: str, max_results: int = None, settings: dict = None) -> str:
    """Search using DuckDuckGo Search library"""
    if max_results is None:
        max_results = (settings or {}).get("max_results") or WEB_SEARCH_DDGS_DEFAULT_RESULTS
    try:
        from ddgs import DDGS  # renamed package: duckduckgo-search is frozen at 8.1.1; ddgs is the maintained successor
        ddgs = DDGS()
        # ddgs 9.x API: text(query, max_results=...) returns a list of {title, href, body} dicts directly.
        # (Old 6-8.x used keywords= and a lite backend; neither argument exists in ddgs 9.x.)
        results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found from DDGS."
        formatted = []
        for i, r in enumerate(results):
            title = r.get('title', 'No title')
            href = r.get('href', r.get('url', 'No URL'))
            body = r.get('body', r.get('description', ''))[:WEB_SEARCH_RESULT_SNIPPET_CHARS]
            formatted.append(f"{i+1}. {title}\n   URL: {href}\n   {body}...")
        return "Search Results (DuckDuckGo):\n\n" + "\n\n".join(formatted)
    except ImportError:
        return "DDGS library not installed"
    except Exception as e:
        logger.warning(f"DDGS search failed: {e}")
        return f"DDGS error: {str(e)}"


def _ddghtml_search(query: str, max_results: int = None, settings: dict = None) -> str:
    """Search using DuckDuckGo HTML interface"""
    if max_results is None:
        max_results = WEB_SEARCH_DEFAULT_MAX_RESULTS
    timeout = (settings or {}).get("timeout") or WEB_SEARCH_DDGS_TIMEOUT
    search_url = f"https://html.duckduckgo.com/html/?q={url_quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Referer': 'https://html.duckduckgo.com/',
    }
    try:
        response = requests.get(search_url, headers=headers, timeout=timeout)
        if response.status_code != 200:
            return f"DuckDuckGo returned status {response.status_code} (blocked)."
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        for result_div in soup.select('.result')[:max_results]:
            link = result_div.select_one('.result__a')
            snippet = result_div.select_one('.result__snippet')
            if link:
                title = link.get_text(strip=True)
                href = link.get('href', '')
                if 'uddg=' in href:
                    try:
                        from urllib.parse import unquote, urlparse, parse_qs
                        parsed = urlparse(href)
                        params_dict = parse_qs(parsed.query)
                        if 'uddg' in params_dict:
                            href = unquote(params_dict['uddg'][0])
                    except Exception:
                        logger.debug("Parse/decode error ignored at tools/web_tools/web_search.py (ddghtml uddg)")
                desc = snippet.get_text(strip=True) if snippet else ''
                if title and href and href.startswith('http'):
                    results.append({'title': title, 'href': href, 'body': desc})
        if not results:
            return "No results found from DuckDuckGo HTML."
        formatted = []
        for i, r in enumerate(results[:max_results]):
            if r['body']:
                formatted.append(f"{i+1}. {r['title']}\n   URL: {r['href']}\n   {r['body']}")
            else:
                formatted.append(f"{i+1}. {r['title']}\n   URL: {r['href']}")
        return "Search Results (DuckDuckGo):\n\n" + "\n\n".join(formatted)
    except Exception as e:
        return f"DuckDuckGo HTML error: {str(e)}"


def _bing_search(query: str, max_results: int = None, settings: dict = None) -> str:
    """Search using Bing HTML interface"""
    if max_results is None:
        max_results = WEB_SEARCH_DEFAULT_MAX_RESULTS
    timeout = (settings or {}).get("timeout") or WEB_SEARCH_BING_TIMEOUT
    search_url = f"https://www.bing.com/search?q={url_quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
    }
    try:
        response = requests.get(search_url, headers=headers, timeout=timeout)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        for li in soup.select('.b_algo')[:max_results]:
            title_elem = li.select_one('h2')
            link_elem = li.select_one('a')
            desc_elem = li.select_one('.b_desc')
            if title_elem and link_elem:
                title = title_elem.get_text(strip=True)
                link = link_elem.get('href', '')
                desc = desc_elem.get_text(strip=True)[:WEB_SEARCH_RESULT_SNIPPET_CHARS] if desc_elem else ''
                if link.startswith('http'):
                    results.append(f"{len(results)+1}. {title}\n   URL: {link}\n   {desc}")
        if results:
            return "Search Results (Bing):\n\n" + "\n\n".join(results)
    except Exception as e:
        logger.debug(f"Bing search failed: {e}")
    return None


def _searx_search(query: str, max_results: int = None, settings: dict = None) -> str:
    """Search using SearXNG instances"""
    if max_results is None:
        max_results = WEB_SEARCH_DEFAULT_MAX_RESULTS
    timeout = (settings or {}).get("timeout") or WEB_SEARCH_SEARXNG_TIMEOUT
    searx_instances = (settings or {}).get("instances") or WEB_SEARCH_SEARXNG_INSTANCES
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    for instance in searx_instances:
        try:
            search_url = f"{instance}/search?q={url_quote(query)}&format=json&engines=google,bing,duckduckgo&safesearch=0"
            response = requests.get(search_url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                if results:
                    formatted = []
                    for i, r in enumerate(results[:max_results]):
                        title = r.get('title', 'No title')
                        url = r.get('url', r.get('pretty_url', 'No URL'))
                        content = r.get('content', '')[:WEB_SEARCH_RESULT_SNIPPET_CHARS] if r.get('content') else ''
                        engine = r.get('engine', 'unknown')
                        formatted.append(f"{i+1}. {title}\n   URL: {url}\n   {content}...\n   [Source: {engine}]")
                    return f"Search Results (SearXNG):\n\n" + "\n\n".join(formatted)
        except Exception as e:
            logger.debug(f"SearXNG instance {instance} failed: {e}")
            continue
    return None


# Adapter registry - the ONLY place that maps engine ids to implementations.
_ENGINE_ADAPTERS = {
    "wikipedia": _wikipedia_search,
    "ddgs": _ddgs_search,
    "ddghtml": _ddghtml_search,
    "bing": _bing_search,
    "searxng": _searx_search,
}


# Per-engine acceptance checks (kept identical to the pre-refactor behavior: each
# engine had its own quirks about which output strings count as a usable result).
def _accept_wikipedia(r):  return r is not None and "No results" not in r and len(r) > 100
def _accept_ddgs(r):       low = (r or "").lower(); return bool(r) and "No results" not in r and "error" not in low and "blocked" not in low and "not installed" not in low
def _accept_ddghtml(r):    low = (r or "").lower(); return bool(r) and "No results" not in r and "error" not in low and "failed" not in low
def _accept_bing(r):       return r is not None and "No results" not in r
def _accept_searxng(r):    return r is not None and "No results" not in r and "error" not in (r or "").lower()

_ENGINE_ACCEPT = {
    "wikipedia": _accept_wikipedia,
    "ddgs": _accept_ddgs,
    "ddghtml": _accept_ddghtml,
    "bing": _accept_bing,
    "searxng": _accept_searxng,
}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def web_search(query: str, max_results: int = None, search_engines=None) -> str:
    """Search the web using the enabled engines (config-driven, priority order)."""
    if max_results is None:
        max_results = WEB_SEARCH_DEFAULT_MAX_RESULTS

    # Clean query by removing common filler phrases
    clean_query = query
    fillers = [
        'i want you to ', 'can you ', 'could you ', 'would you ', 'please ',
        'tell me ', 'what is ', 'what was ', 'who is ', 'who was ',
        'search for ', 'search ', 'google ', 'find '
    ]
    for f in fillers:
        clean_query = clean_query.lower().replace(f, ' ')
    clean_query = ' '.join(clean_query.split())

    search_queries = []
    if clean_query and len(clean_query) > 3 and clean_query.lower() != query.lower():
        search_queries.append(clean_query)
    search_queries.append(query)

    engines = _normalize_search_engines(search_engines)
    active = [e for e in sorted(engines, key=lambda x: x.get("priority", 99)) if e.get("enabled") is True]
    active_ids = []
    for e in active:
        eid = e.get("id")
        if eid not in _ENGINE_ADAPTERS:
            logger.warning(f"[SEARCH] Unknown engine id '{eid}' in config - skipping (add an adapter in web_search.py)")
            continue
        active_ids.append(eid)

    logger.info(f"[SEARCH] Searching with queries: {search_queries}, engines: {active_ids}")

    if not active_ids:
        return "Search failed. No search engines are enabled (see Settings -> Web Search Engines or config/search_engines.json)."

    errors = []
    for sq in search_queries:
        for e in active:
            eid = e.get("id")
            adapter = _ENGINE_ADAPTERS.get(eid)
            accept = _ENGINE_ACCEPT.get(eid)
            if adapter is None or accept is None:
                continue
            try:
                result = adapter(sq, max_results, _engine_settings(e))
                if not accept(result):
                    errors.append(f"{eid}: {(result or 'no results')[:80]}")
                    continue
                return result
            except Exception as ex:
                errors.append(f"{eid} error: {str(ex)[:80]}")

    return f"Search failed. Errors: {' | '.join(errors)}"
