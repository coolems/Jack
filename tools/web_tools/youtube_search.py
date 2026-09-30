"""YouTube search function - searches for videos using Piped API with YouTube HTML fallback

FIX (2026-08-23): the old version hardcoded two dead Piped instances and swallowed all
errors, so it ALWAYS returned "No YouTube videos found". Now:
  * instance list is configurable via the PIPED_INSTANCES env var (comma-separated)
    or tools/config/piped_instances.json; defaults include verified-live mirrors.
  * every failing instance reports its real status code / error in the output.
  * if ALL Piped instances fail, falls back to parsing YouTube's own results page
    (ytInitialData JSON -- same approach web_search uses for HTML engines).
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "youtube_search",
        "description": "Search YouTube for videos using the Piped API. Returns video titles, URLs, channel names, and durations. Use for finding tutorials, trailers, reviews, or specific videos. Do NOT use for playing videos or extracting transcripts - returns metadata only. Use concise, specific queries.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for YouTube videos. Example: 'qwen3.6 tutorial', 'NASA artemis mission trailer'"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (1-20). Default: 10.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 20
                }
            },
            "required": ["query"]
        }
    }
}

import json
import logging
import os
import re
import urllib.parse

import requests

logger = logging.getLogger("COOLEMS.Tools.Web")

# Verified-live Piped mirrors (checked 2026-08-23). Order matters: first live one wins.
DEFAULT_PIPED_INSTANCES = [
    "https://api.piped.private.coffee",
    "https://pipedapi.ducks.party",
]

_BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
}


def _load_piped_instances():
    """Resolve the Piped instance list: env var > JSON config file > defaults."""
    env = os.environ.get("PIPED_INSTANCES", "").strip()
    if env:
        return [u.strip().rstrip('/') for u in env.split(',') if u.strip()]

    # Optional repo-local override (tools/config/piped_instances.json) so instances
    # can be refreshed without a code change. Path is relative to this file's package.
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "piped_instances.json")
    try:
        if os.path.isfile(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                data = json.load(f)
            instances = [u.strip().rstrip('/') for u in data.get("instances", []) if isinstance(u, str) and u.strip()]
            if instances:
                return instances
    except Exception as e:
        logger.warning(f"Could not read piped_instances.json ({e}); using defaults")

    return list(DEFAULT_PIPED_INSTANCES)


def _search_piped(query, max_results):
    """Try every configured Piped instance. Returns (results, per_instance_report)."""
    results = []
    report = []  # [(instance, status_text), ...] in tried order

    for instance in _load_piped_instances():
        search_url = f"{instance}/search?q={urllib.parse.quote(query)}&filter=videos"
        try:
            response = requests.get(search_url, headers=_BROWSER_HEADERS, timeout=15)
            if response.status_code == 200:
                data = response.json()
                items = data.get('items', data.get('results', []))

                for item in items[:max_results]:
                    if item.get('type') == 'stream' or item.get('url'):
                        video_id = item.get('url', '').replace('/watch?v=', '')
                        if not video_id:
                            video_id = item.get('id', '')
                        title = item.get('title', item.get('name', 'No title'))
                        author = item.get('uploaderName', item.get('uploader', 'Unknown'))
                        duration = item.get('duration', 0)
                        results.append({
                            'title': title,
                            'url': f"https://www.youtube.com/watch?v={video_id}",
                            'channel': author,
                            'duration': str(duration) if duration else 'N/A',
                        })

                report.append((instance, "200 OK"))
                if results:
                    break  # first live instance with results wins
            else:
                report.append((instance, f"HTTP {response.status_code}"))
        except requests.exceptions.Timeout:
            report.append((instance, "timeout (15s)"))
        except Exception as e:
            report.append((instance, f"{type(e).__name__}: {str(e)[:80]}"))

    return results[:max_results], report


def _search_youtube_html(query, max_results):
    """Fallback: parse YouTube's own results page (ytInitialData JSON)."""
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query) + "&hl=en"
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
        'Accept-Language': 'en-US,en;q=0.9',
    }
    response = requests.get(url, headers=headers, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"YouTube results page returned HTTP {response.status_code}")

    # YouTube always serves UTF-8; force it so titles with special chars decode right.
    response.encoding = "utf-8"

    match = re.search(r'var ytInitialData\s*=\s*(\{.*?\});</script>', response.text, re.DOTALL)
    if not match:
        raise RuntimeError("ytInitialData block not found in YouTube results page")

    data = json.loads(match.group(1))
    results = []

    def _walk(obj):
        """Depth-first search for every videoRenderer node."""
        if isinstance(obj, dict):
            if 'videoRenderer' in obj:
                v = obj['videoRenderer']
                video_id = v.get('videoId', '')
                title_runs = v.get('title', {}).get('runs', [])
                title = ''.join(r.get('text', '') for r in title_runs) or 'No title'
                try:
                    channel = v['ownerText']['runs'][0]['text']
                except (KeyError, IndexError):
                    channel = 'Unknown'
                duration = ''
                try:
                    duration = v['lengthText'].get('simpleText', '')
                except AttributeError:
                    pass
                if video_id:
                    results.append({
                        'title': title,
                        'url': f"https://www.youtube.com/watch?v={video_id}",
                        'channel': channel,
                        'duration': duration or 'N/A',
                    })
            for val in obj.values():
                _walk(val)
        elif isinstance(obj, list):
            for it in obj:
                _walk(it)

    _walk(data)
    return results[:max_results]


def youtube_search(query: str, max_results: int = 10) -> str:
    """Search YouTube for videos (Piped API first, YouTube HTML fallback)."""
    # Primary path: Piped mirrors.
    results, report = _search_piped(query, max_results)

    if not results:
        # Fallback: YouTube's own search page.
        try:
            logger.info("All Piped instances failed; falling back to YouTube HTML parsing")
            results = _search_youtube_html(query, max_results)
        except Exception as e:
            details = "; ".join(f"{inst} -> {status}" for inst, status in report) or "no instances configured"
            return (f"No YouTube videos found. All sources failed.\n"
                    f"Piped instances tried: {details}\n"
                    f"YouTube HTML fallback error: {e}")

    formatted = []
    for i, r in enumerate(results[:max_results], 1):
        formatted.append(f"{i}. {r['title']}\n   URL: {r['url']}\n   Channel: {r['channel']} | Duration: {r['duration']}")

    source_note = "" if report and report[-1][1] == "200 OK" else "\n(source: YouTube HTML fallback)"
    return f"YouTube Search Results ({len(results[:max_results])} videos):{source_note}\n\n" + "\n\n".join(formatted)
