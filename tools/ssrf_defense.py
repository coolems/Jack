"""Deprecated SSRF shim kept only so delivered tool code keeps importing.

tools/ssrf_guard.py enforces the same policy at the transport layer: the IP that gets
validated is the exact IP the socket connects to, by construction (per-session
PinnedConnector for aiohttp; no global state, no threads, asyncio-only). See that module
for the full contract.

Keep the 'ssrf_guard' key BEFORE 'ssrf_defense' in the SERVER shared-source list
(app/providers/coolems/tool_scanner/shared_sources.py::get_all_shared_sources) - this
module imports from it at exec time.
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
    """Legacy stub: logs and raises SSRFError, so a leftover caller fails loudly.

    Address pinning happens inside the HTTP client per session via safe_session() /
    PinnedConnector; global getaddrinfo monkeypatching is not used.
    """
    _logger.error(
        "pinned_getaddrinfo() was removed in the 2026-09 ssrf_guard rewrite -- "
        f"unprotected request refused for {url!r}. Use tools.ssrf_guard.safe_session()."
    )
    raise SSRFError("pinned_getaddrinfo is removed; use tools.ssrf_guard.safe_session()")
