"""DEPRECATED SHIM -- the real SSRF defense now lives in tools/ssrf_guard.py.

(2026-09 rewrite) The old implementation closed the DNS-rebinding / TOCTOU window by
monkeypatching socket.getaddrinfo for the duration of a request: global, process-wide
state with non-atomic save/restore that could not be made safe under concurrency (two
overlapping pins destroyed or leaked each other -- a live SSRF bypass). It is gone.

tools/ssrf_guard.py enforces the same policy at the transport layer instead: the IP
that gets validated is the exact IP the socket connects to, by construction (per-session
PinnedConnector for aiohttp; no global state, no threads, asyncio-only). See that
module's docstring for the full contract.

This shim exists so code written against the old module keeps working unchanged:
  * tools/web_tools/fetch_url.py        -- imports ensure_url_not_ssrff (now from ssrf_guard)
  * CLIENT-delivered tool code          -- 'from tools.ssrf_defense import X' rewrites
    by the loader to this module's symbols, which are re-exports of ssrf_guard's.

DELIVERY NOTE: keep BOTH keys in the shared-source lists on the SERVER
(app/providers/coolems/tool_scanner/shared_sources.py::get_all_shared_sources and
tools_server/local_tool_scanner.py::_load_shared_modules), ordered 'ssrf_guard' BEFORE
'ssrf_defense' -- this module imports from it at exec time.

The old pinned_getaddrinfo() context manager is NOT re-exported: its replacement is the
transport-level pin (safe_session()/PinnedConnector). If any delivered code still calls
it, fail loudly instead of pretending to protect anything.
"""

import logging as _logging

# Re-export the public API from the new single source of truth.
from tools.ssrf_guard import (  # noqa: F401
    SSRFError,
    validate_url_not_ssrff,
    ensure_url_not_ssrff,
    is_private_ip,
    is_blocked_ip_obj,
    is_localhost_ip,
    safe_session,
    PinnedConnector,
)

_logger = _logging.getLogger("COOLEMS.SSRFDefense")


def pinned_getaddrinfo(url):  # noqa: N802 -- legacy name kept for a loud failure only
    """REMOVED (2026-09). The global getaddrinfo monkeypatch was unsafe under concurrency.

    Use safe_session() / PinnedConnector instead -- pinning now happens inside the HTTP
    client, per session, with no global state. This stub raises so any leftover caller
    fails loudly rather than running an unprotected request silently (fail-closed).
    """
    _logger.error(
        "pinned_getaddrinfo() was removed in the 2026-09 ssrf_guard rewrite -- "
        f"unprotected request refused for {url!r}. Use tools.ssrf_guard.safe_session()."
    )
    raise SSRFError("pinned_getaddrinfo is removed; use tools.ssrf_guard.safe_session()")
