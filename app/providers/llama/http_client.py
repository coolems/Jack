"""
Shared HTTP client pool for llama.cpp provider.

Centralizes all HTTP connections to llama-server into a single pooled async
client (httpx) and a single sync session (requests). This eliminates the old
pattern of creating/destroying clients on every request, which wasted TCP
handshakes and TLS negotiation under load.
"""
import logging
from typing import Optional

import httpx
import requests

logger = logging.getLogger("COOLEMS.Provider.Llama.HTTPPool")


# ---------------------------------------------------------------------------
# Pool configuration — defaults used regardless of config.py constants
# ---------------------------------------------------------------------------
HTTP_POOL_MAX_CONNECTIONS: int = 20
HTTP_POOL_KEEPALIVE_CONNECTIONS: int = 10


# ---------------------------------------------------------------------------
# Async client (httpx) — used by streaming, non_streaming, ocr
# ---------------------------------------------------------------------------

# One pooled httpx.AsyncClient PER RUNNING EVENT LOOP.
# WHY (2026-09 threadless refactor): boot runs in code.py/code_client.py's asyncio.run(_boot())
# and the relay/uvicorn then run a SEPARATE loop afterwards - an httpx.AsyncClient is bound to
# the loop it was created on, so a single global client would be dead (or crash) once the boot
# loop closed. Keying by the running loop gives every loop its own pool; pools of loops that
# have exited are pruned lazily and GC'd. No threads involved - each entry belongs to exactly
# one loop and is only touched from that loop's context.

import asyncio as _asyncio

_async_clients: dict = {}   # event_loop -> httpx.AsyncClient


def _prune_closed_loops() -> None:
    """Drop pools whose owning loop has closed (e.g. the boot loop after boot finished).

    The dropped client is simply released to GC - closing an httpx client from a DIFFERENT
    loop than it was created on raises, so we do not attempt it; any idle keep-alive sockets
    die with their loop's transport teardown / OS timeout."""
    for lp in [l for l in list(_async_clients) if l.is_closed()]:
        _async_clients.pop(lp, None)


async def get_async_client() -> httpx.AsyncClient:
    """Return the pooled async HTTP client for THE CURRENT event loop.

    Creates it lazily on first use per loop; each loop gets its own pool so boot-loop and
    relay-loop clients never cross (httpx clients are loop-bound)."""
    _prune_closed_loops()
    loop = _asyncio.get_running_loop()
    client = _async_clients.get(loop)
    # NOTE: httpx exposes `is_closed` as a PROPERTY (bool) - do not call it.
    if client is None or client.is_closed:
        limits = httpx.Limits(
            max_connections=HTTP_POOL_MAX_CONNECTIONS,
            max_keepalive_connections=HTTP_POOL_KEEPALIVE_CONNECTIONS,
        )
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=limits,
        )
        _async_clients[loop] = client
        logger.info("Created shared async HTTP client (httpx) with connection pool")
    return client


async def close_async_client() -> None:
    """Gracefully close THE CURRENT loop's pooled client. Idempotent.

    Called from LlamaProvider.close() on the relay loop - it closes only that loop's pool;
    other loops' pools are pruned by _prune_closed_loops() when their loops exit."""
    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop - nothing to close here
    client = _async_clients.pop(loop, None)
    if client is not None and not client.is_closed:  # property access - see get_async_client note
        await client.aclose()
        logger.info("Closed shared async HTTP client")


# ---------------------------------------------------------------------------
# Sync session (requests) — legacy, kept for compatibility.
# (2026-09 threadless refactor): HealthChecker is async now and uses the pooled httpx client;
# nothing in the provider path calls get_sync_session() anymore.
# ---------------------------------------------------------------------------

_sync_session: Optional[requests.Session] = None


def get_sync_session() -> requests.Session:
    """Return the shared sync HTTP session (creates lazily on first call)."""
    global _sync_session
    if _sync_session is None:
        _sync_session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=0,  # we handle retries ourselves
        )
        _sync_session.mount("http://", adapter)
        _sync_session.mount("https://", adapter)
        logger.info("Created shared sync HTTP session (requests) with connection pool")
    return _sync_session


def close_sync_session() -> None:
    """Close the shared sync session. Idempotent."""
    global _sync_session
    if _sync_session is not None:
        _sync_session.close()
        logger.info("Closed shared sync HTTP session")
    _sync_session = None
