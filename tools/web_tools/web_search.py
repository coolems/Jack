"""web_search - concurrent multi-provider web search (config-driven, intent-routed).

Providers are declared in <CLIENT>/config/search_engines.json (single source of
truth, editable in the Settings UI). Each entry looks like:

    {"id": "bing_rss", "name": "Bing RSS", "enabled": true, "priority": 1,
     "settings": {"timeout": 10}}

- ``search_engines=None``        -> read <CLIENT>/config/search_engines.json live
- ``search_engines={"engines":[...]}`` or a bare list of engine dicts -> use as-is
- legacy bool dict ``{"wikipedia": true, ...}`` is still accepted (old clients)

To add a provider: add its entry to search_engines.json AND register an adapter in
_search_providers.PROVIDERS. Unknown ids are logged and skipped - never fatal.

Design (2026-11-06 rewrite): every enabled provider for this query runs CONCURRENTLY
under ONE overall deadline, results are merged round-robin and deduped, and a
provider only counts as usable when it parsed real results. The previous version ran
engines sequentially, sent the query twice, and accepted a blocked HTTP 202 page as a
result - that is what made searches slow and silently empty.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the internet for current information, news, live data, or unknown facts. Runs several engines concurrently and merges the results. Use BEFORE fetching URLs or browsing. Do NOT use for local file operations or math.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Concise search query, without filler words."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Results to return (1-20). Default 10.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 20
                },
                "freshness": {
                    "type": "string",
                    "enum": ["Day", "Week", "Month", "Year"],
                    "description": "Keep only results published within this window."
                }
            },
            "required": ["query"]
        }
    }
}

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

from ._search_core import (
    clean_query,
    clean_snippet,
    dedup_results,
    format_results,
    normalize_url,
    query_tokens,
    rank_by_relevance,
    route_providers,
    select_relevant,
    spill_report,
)
from ._search_providers import PROVIDERS

logger = logging.getLogger("COOLEMS.Tools.Web")

MAX_PROVIDERS = 6
OVERALL_DEADLINE_SEC = 12
OUTPUT_MAX_CHARS = 8000
SNIPPET_CHARS = 300

# Freshness windows in days, applied to provider-supplied publication dates.
_FRESHNESS_DAYS = {"Day": 1, "Week": 7, "Month": 30, "Year": 365}

# Readiness gate (2026-11-06): a provider that failed repeatedly is skipped for a
# cooldown instead of being retried on every search. Engines are dead on many
# machines (ddgs not installed, DDG HTML behind a bot wall, SearXNG instances down),
# and the old sequential chain burned a full timeout on each of them. A provider that
# succeeds again is put back immediately; the gate never locks a provider out forever.
_HEALTH: dict = {}
_HEALTH_MAX_FAILURES = 3
_HEALTH_COOLDOWN_SEC = 300


def _provider_ready(provider_id: str) -> bool:
    state = _HEALTH.get(provider_id)
    if state is None:
        return True
    failures, last_failure = state
    if failures < _HEALTH_MAX_FAILURES:
        return True
    return (time.time() - last_failure) >= _HEALTH_COOLDOWN_SEC


def _record_outcome(provider_id: str, ok: bool) -> None:
    if ok:
        _HEALTH.pop(provider_id, None)
        return
    failures, last_failure = _HEALTH.get(provider_id, (0, time.time()))
    _HEALTH[provider_id] = (failures + 1, time.time())


def _const(name: str, default):
    """Config constant if delivered, else the module default.

    CLIENT tools receive SERVER config constants as injected globals (the
    'from config import ...' line is stripped during delivery), so reading them
    through globals() works on both sides without inventing an engine fallback.
    """
    value = globals().get(name)
    return value if value is not None else default


# ---------------------------------------------------------------------------
# Config plumbing (search_engines.json is the single source of truth)
# ---------------------------------------------------------------------------

def _load_engine_config_from_json() -> list:
    """Read <CLIENT>/config/search_engines.json live. Returns [] on any problem.

    Path resolution (the CLIENT owns this file - no fallback providers are invented):
      1. <CWD>/config/search_engines.json       - normal CLIENT run (CWD = CLIENT dir)
      2. <local_ai>/CLIENT/config/...           - SERVER-side layout
    """
    try:
        import json as _json

        candidates = [os.path.join(os.getcwd(), "config", "search_engines.json")]
        try:
            module_dir = os.path.dirname(os.path.abspath(__file__))
            local_ai_root = os.path.dirname(os.path.dirname(module_dir))
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
        with open(path, "r", encoding="utf-8") as handle:
            data = _json.load(handle)
        engines = data.get("engines") if isinstance(data, dict) else None
        return [e for e in (engines or []) if isinstance(e, dict)]
    except Exception as exc:
        logger.error(f"search_engines.json missing or invalid ({exc}) - no search providers "
                     f"available. Fix the file (looked in: {', '.join(candidates)})")
        return []


def _normalize_search_engines(search_engines) -> list:
    """Normalize any accepted input form into an ordered [{id, enabled, settings}, ...]."""
    if search_engines is None:
        engines = _load_engine_config_from_json()
        return [e for e in engines if isinstance(e.get("enabled"), bool)]

    if isinstance(search_engines, dict):
        engines = search_engines.get("engines")
        if isinstance(engines, list):
            return [e for e in engines if isinstance(e, dict)]
        known = set(PROVIDERS.keys())
        result = []
        for index, (engine_id, enabled) in enumerate(search_engines.items(), 1):
            if engine_id not in known:
                continue
            result.append({"id": engine_id, "enabled": bool(enabled),
                           "priority": index, "settings": {}})
        return result

    if isinstance(search_engines, list):
        return [e for e in search_engines if isinstance(e, dict)]

    logger.error(f"Unrecognized search_engines format ({type(search_engines).__name__}) "
                 f"- no providers used. Fix config/search_engines.json")
    return []


def _provider_settings(engine: dict) -> dict:
    """Per-provider settings from the JSON entry (timeout, searxng instances)."""
    settings = engine.get("settings") or {}
    if not isinstance(settings, dict):
        settings = {}
    out = {}
    timeout = settings.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        out["timeout"] = float(min(timeout, OVERALL_DEADLINE_SEC))
    if engine.get("id") == "searxng":
        instances = settings.get("instances") or _const("WEB_SEARCH_SEARXNG_INSTANCES", [])
        if isinstance(instances, list) and instances and all(isinstance(x, str) for x in instances):
            out["instances"] = [x.rstrip("/") for x in instances]
    return out


# ---------------------------------------------------------------------------
# Raw provider items -> model-facing items
# ---------------------------------------------------------------------------

def _normalize_items(raw_items, provider_id: str) -> list:
    """Normalise URLs, clean snippets and tag the provider on each item.

    Providers return raw dicts (they never import _search_core - a shared_dep cannot
    import another shared_dep on the CLIENT delivery path), so this is where the
    redirect wrappers get unwrapped and the snippets get entity-cleaned.
    """
    snippet_chars = _const("WEB_SEARCH_RESULT_SNIPPET_CHARS", SNIPPET_CHARS)
    items = []
    for raw in raw_items:
        url = normalize_url(raw.get("url") or "")
        if not url:
            continue
        items.append({
            "title": clean_snippet(raw.get("title") or "", 160) or "(no title)",
            "url": url,
            "snippet": clean_snippet(raw.get("snippet") or "", snippet_chars),
            "date": raw.get("date") or "",
            "provider": provider_id,
        })
    return items


# ---------------------------------------------------------------------------
# Freshness filter (deterministic: applied to provider publication dates)
# ---------------------------------------------------------------------------

def _parse_date(value: str):
    """Parse an RSS pubDate / ISO timestamp into an aware datetime, or None."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except Exception:
        pass
    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            from datetime import timezone
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _apply_freshness(items, freshness: str) -> list:
    """Keep items published inside the window; items with no date are dropped.

    Bing's own freshness parameter is unreliable (measured 2026-11-06: freshness=Day
    returned 2024 pages), so the window is enforced here on the returned dates.
    """
    days = _FRESHNESS_DAYS.get(freshness)
    if not days:
        return items
    from datetime import timedelta, timezone
    cutoff = time.time() - days * 86400
    kept = []
    for item in items:
        parsed = _parse_date(item.get("date") or "")
        if parsed is None:
            continue
        if parsed.timestamp() >= cutoff:
            kept.append(item)
    return kept


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def web_search(query: str, max_results: int = None, freshness: str = None,
               search_engines=None) -> str:
    """Search the web with the enabled providers, concurrently, one shared deadline."""
    if not isinstance(query, str) or not query.strip():
        return "Error: web_search needs a non-empty query string."

    cleaned = clean_query(query)
    limit = max_results if isinstance(max_results, int) and not isinstance(max_results, bool) else None
    # Schema default is 10; WEB_SEARCH_DEFAULT_MAX_RESULTS is the ceiling, not the default.
    ceiling = _const("WEB_SEARCH_DEFAULT_MAX_RESULTS", 20)
    limit = max(1, min(int(limit or 10), int(ceiling), 20))

    if freshness is not None:
        if freshness not in _FRESHNESS_DAYS:
            return (f"Error: freshness must be one of Day, Week, Month, Year "
                    f"(got {freshness!r}).")

    engines = _normalize_search_engines(search_engines)
    enabled_order = [e for e in sorted(engines, key=lambda x: x.get("priority", 99))
                     if e.get("enabled") is True]

    unknown = [e.get("id") for e in enabled_order if e.get("id") not in PROVIDERS]
    if unknown:
        logger.warning(f"[SEARCH] Unknown provider id(s) in config: {unknown} - skipping "
                       f"(add an adapter in _search_providers.py)")
    enabled_ids = [e.get("id") for e in enabled_order if e.get("id") in PROVIDERS]

    if not enabled_ids:
        return ("Search failed. No search providers are enabled "
                "(see Settings -> Web Search Engines or config/search_engines.json).")

    selected = route_providers(cleaned, enabled_ids)[: MAX_PROVIDERS]
    if not selected:
        # No general engine enabled and no intent match: use the enabled specialists.
        selected = enabled_ids[: MAX_PROVIDERS]

    settings_by_id = {e.get("id"): _provider_settings(e) for e in enabled_order}
    per_provider_limit = max(3, min(limit, 10))

    logger.info(f"[SEARCH] '{cleaned}' -> providers: {selected}")

    notes = []

    # Readiness gate: providers that failed _HEALTH_MAX_FAILURES times in a row are
    # skipped until the cooldown expires, so a dead engine costs nothing per search.
    ready = [pid for pid in selected if _provider_ready(pid)]
    skipped = [pid for pid in selected if pid not in ready]
    if skipped:
        notes.append("skipped (recently failing): " + ", ".join(skipped))
    if not ready:
        # Everything is cooling down - try the enabled providers once more rather
        # than returning nothing, and let the outcomes update the health table.
        ready = enabled_ids[: MAX_PROVIDERS]
        notes.append("all providers recently failing - retried anyway")

    groups = []
    used = []

    def _run(provider_id: str):
        adapter = PROVIDERS[provider_id]
        try:
            return provider_id, adapter(cleaned, per_provider_limit, settings_by_id.get(provider_id, {}))
        except Exception as exc:
            return provider_id, {"ok": False, "items": [],
                                 "note": f"{provider_id}: {type(exc).__name__}: {str(exc)[:60]}"}

    deadline = _const("WEB_SEARCH_OVERALL_DEADLINE_SEC", OVERALL_DEADLINE_SEC)
    started = time.time()
    pool = ThreadPoolExecutor(max_workers=len(ready))
    try:
        futures = {pool.submit(_run, pid): pid for pid in ready}
        # One shared deadline for the whole search - no sequential engine chain.
        for future, pid in futures.items():
            remaining = deadline - (time.time() - started)
            try:
                provider_id, outcome = future.result(timeout=max(0.1, remaining))
            except Exception as exc:
                _record_outcome(pid, False)
                notes.append(f"{pid}: timed out or failed ({type(exc).__name__})")
                continue
            _record_outcome(provider_id, bool(outcome.get("ok")))
            if outcome.get("ok"):
                groups.append(_normalize_items(outcome["items"], provider_id))
                used.append(provider_id)
            else:
                notes.append(outcome.get("note") or f"{pid}: no results")
    finally:
        # Do NOT wait for stragglers: a ThreadPoolExecutor's implicit shutdown(wait=True)
        # made the call run far past the deadline (measured 2026-11-06: 54 s for a 12 s
        # budget, because ddgs retries several backends internally). Cancelling keeps the
        # call bounded by WEB_SEARCH_OVERALL_DEADLINE_SEC.
        pool.shutdown(wait=False, cancel_futures=True)

    unique, total_raw = dedup_results(groups)
    if freshness:
        filtered = _apply_freshness(unique, freshness)
        dropped = len(unique) - len(filtered)
        if dropped:
            notes.append(f"freshness={freshness}: dropped {dropped} result(s) whose "
                         f"publication date was missing or outside the window")
        unique = filtered

    # Rank by relevance before spending the budget: engines return geo/localisation
    # junk for technical queries, and a round-robin merge would otherwise put that
    # junk in the visible slots. Junk is only dropped when enough real matches exist.
    tokens = query_tokens(cleaned)
    ranked, low_relevance = rank_by_relevance(unique, tokens)
    items, dropped_low = select_relevant(ranked, tokens, limit)
    truncated = total_raw > len(items) or len(unique) > len(items)

    report = format_results(cleaned, items, used, truncated, notes, low_relevance + dropped_low)
    return spill_report(report, _working_root(), _const("WEB_SEARCH_OUTPUT_MAX_CHARS", OUTPUT_MAX_CHARS))


def _working_root() -> str:
    """working_root for the spill file; utils is a delivered shared module."""
    utils = globals().get("utils")
    getter = getattr(utils, "get_working_root", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            pass
    return os.getcwd()
