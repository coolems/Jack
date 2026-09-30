"""Connection enforcement — per-API-key connection counting and token-bucket rate limiting.

Two classes:
  1. ConnectionTracker   — counts active WebSocket connections per API key.
     Rejects new connections when max_connections is reached. Cleans up on disconnect.

  2. TokenBucketLimiter  — classic token-bucket algorithm per API key.
     Allows burst up to the bucket capacity, then refills at max_rate_limit tokens/sec.

Both are asyncio-safe (use asyncio.Lock). Designed as module-level singletons so they
share state across all WebSocket handler coroutines on the same event loop.

Usage in the direct WS handlers (direct_ws package, ex-ws_client_handler):
    # Auth time — check connection limit
    user_info = gateway.authenticate(api_key)
    max_conn = user_info.get("max_connections", 1)
    if not conn_tracker.can_connect(api_key, max_conn):
        reject_connection()
    conn_id = conn_tracker.register(api_key, ws)

    # Before processing each chat request — check rate limit
    rate_limit = user_info.get("max_rate_limit", 30)
    if not await rate_limiter.allow_request(api_key, rate_limit):
        send_error("Rate limit exceeded")
        continue

    # On disconnect — clean up
    conn_tracker.unregister(conn_id)

Lifecycle:
    # Server shutdown (called from server_provider.stop_all_servers())
    await rate_limiter.stop()
"""

import asyncio
import time
import logging
from typing import Dict, Optional, Set, Tuple

logger = logging.getLogger("COOLEMS.Provider.ConnectionEnforcement")


# ---------------------------------------------------------------------------
# Connection Tracker
# ---------------------------------------------------------------------------

class ConnectionTracker:
    """Track active WebSocket connections per API key. Enforce max_connections limit."""

    def __init__(self):
        # api_key -> set of connection IDs
        self._connections: Dict[str, Set[str]] = {}
        # conn_id -> (api_key, ws_reference) for reverse lookup and cleanup
        self._conn_map: Dict[str, Tuple[str, object]] = {}
        self._lock = asyncio.Lock()

    @property
    def total_active(self) -> int:
        """Total active connections across all API keys."""
        return len(self._conn_map)

    async def can_connect(self, api_key: str, max_connections: int) -> bool:
        """Check if the API key can open another connection.

        Args:
            api_key: The authenticated API key string.
            max_connections: Maximum allowed concurrent connections for this key's profile.

        Returns:
            True if a new connection is allowed, False if limit reached.
        """
        async with self._lock:
            current = len(self._connections.get(api_key, set()))
            return current < max_connections

    async def register(self, api_key: str, ws: object) -> Optional[str]:
        """Register a new connection for an API key.

        Args:
            api_key: The authenticated API key string.
            ws: The WebSocket connection object (stored weakly for reference).

        Returns:
            A unique connection ID string, or None if registration failed.
        """
        import uuid
        conn_id = f"conn_{uuid.uuid4().hex[:12]}"

        async with self._lock:
            if api_key not in self._connections:
                self._connections[api_key] = set()
            self._connections[api_key].add(conn_id)
            self._conn_map[conn_id] = (api_key, ws)

        logger.info(
            f"[CONN-TRACK] Registered {conn_id} for key={api_key[:8]}... "
            f"(active={len(self._connections[api_key])})"
        )
        return conn_id

    async def unregister(self, conn_id: str):
        """Remove a connection (called on disconnect).

        Args:
            conn_id: The connection ID returned by register().
        """
        async with self._lock:
            entry = self._conn_map.pop(conn_id, None)
            if not entry:
                return

            api_key, _ws = entry
            conns = self._connections.get(api_key)
            if conns:
                conns.discard(conn_id)
                if not conns:  # Clean up empty sets
                    del self._connections[api_key]

        logger.info(f"[CONN-TRACK] Unregistered {conn_id} for key={entry[0][:8]}...")

    async def unregister_all_for_key(self, api_key: str) -> int:
        """Remove ALL connections for an API key (e.g., key revoked).

        Returns the number of connections removed.
        """
        async with self._lock:
            conns = self._connections.pop(api_key, set())
            count = len(conns)
            for conn_id in conns:
                self._conn_map.pop(conn_id, None)
            return count

    async def get_active_count(self, api_key: str) -> int:
        """Get the number of active connections for an API key."""
        async with self._lock:
            return len(self._connections.get(api_key, set()))


# ---------------------------------------------------------------------------
# Token Bucket Rate Limiter
# ---------------------------------------------------------------------------

class _BucketState:
    """Internal state for a single token bucket."""
    __slots__ = ('tokens', 'last_refill')

    def __init__(self, tokens: float):
        self.tokens = tokens
        self.last_refill = time.monotonic()


class TokenBucketLimiter:
    """Token-bucket rate limiter per API key.

    Algorithm:
      - Each key starts with a full bucket (capacity = max_rate_limit).
      - On each request, refill tokens based on elapsed time since last check.
      - If tokens >= 1, consume one and allow the request.
      - Otherwise reject (rate limit exceeded).

    This allows burst up to the bucket capacity, then smooths to rate/sec.

    Cleanup task lifecycle:
      - Lazy-started via start_cleanup() on first auth event.
      - Protected by asyncio.Lock so only ONE cleanup task exists at a time.
      - Properly stopped via stop() during server shutdown.
    """

    def __init__(self):
        # api_key -> _BucketState
        self._buckets: Dict[str, _BucketState] = {}
        self._lock = asyncio.Lock()
        # Periodic cleanup of stale buckets (keys not seen for 5+ minutes)
        self._cleanup_task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()

    async def start_cleanup(self):
        """Start the background cleanup task to remove stale buckets.

        Safe to call multiple times — only one cleanup task will ever run at a time.
        Uses an asyncio.Lock to prevent race conditions if called concurrently from
        different handlers (direct WS + brain relay).
        """
        async with self._start_lock:
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(
                    self._periodic_cleanup(), name="rate-limiter-cleanup"
                )
                logger.info("[RATE-LIMIT] Background cleanup task started (60s interval)")

    async def _periodic_cleanup(self):
        """Run every 60 seconds, remove buckets idle for > 300 seconds."""
        while True:
            await asyncio.sleep(60)
            try:
                await self._cleanup_stale()
            except asyncio.CancelledError:
                logger.info("[RATE-LIMIT] Background cleanup task cancelled (shutdown)")
                break
            except Exception as e:
                logger.debug(f"[RATE-LIMIT] Cleanup error: {e}")

    async def _cleanup_stale(self):
        """Remove buckets that haven't been accessed in 5 minutes."""
        now = time.monotonic()
        # B5 (2026-09-02): collect AND delete under the SAME lock hold -- deleting
        # outside the lock broke the "all dict ops under lock" invariant the rest of
        # this class follows (safe today on a single loop, fragile by design).
        async with self._lock:
            stale_keys = [
                key for key, bucket in self._buckets.items()
                if now - bucket.last_refill > 300
            ]
            for key in stale_keys:
                del self._buckets[key]

        if stale_keys:
            logger.debug(f"[RATE-LIMIT] Cleaned up {len(stale_keys)} stale buckets")

    async def allow_request(self, api_key: str, max_rate_limit: int) -> bool:
        """Check if a request from this API key should be allowed.

        Args:
            api_key: The authenticated API key string.
            max_rate_limit: Tokens per second (from profile). Also the bucket capacity.

        Returns:
            True if request is allowed, False if rate limited.
        """
        now = time.monotonic()

        async with self._lock:
            bucket = self._buckets.get(api_key)

            if bucket is None:
                # First request — start with full bucket (allows initial burst)
                bucket = _BucketState(tokens=float(max_rate_limit))
                self._buckets[api_key] = bucket

            # Refill tokens based on elapsed time
            elapsed = now - bucket.last_refill
            bucket.tokens += elapsed * max_rate_limit
            # Cap at capacity (prevents unlimited accumulation)
            if bucket.tokens > float(max_rate_limit):
                bucket.tokens = float(max_rate_limit)
            bucket.last_refill = now

            # Try to consume one token
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            else:
                logger.debug(
                    f"[RATE-LIMIT] Key={api_key[:8]}... rejected "
                    f"(tokens={bucket.tokens:.2f}/{max_rate_limit})"
                )
                return False

    async def get_remaining_tokens(self, api_key: str) -> float:
        """Get remaining tokens for an API key (for monitoring/debugging)."""
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(api_key)
            if not bucket:
                return 0.0
            # Can't calculate accurate remaining without knowing max_rate_limit from profile
            return bucket.tokens

    async def stop(self):
        """Stop the cleanup background task.

        Called during server shutdown to properly clean up the asyncio.Task.
        Safe to call multiple times or when no task is running.
        """
        async with self._start_lock:
            if self._cleanup_task and not self._cleanup_task.done():
                logger.info("[RATE-LIMIT] Stopping background cleanup task...")
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    pass
                logger.info("[RATE-LIMIT] Background cleanup task stopped")


# ---------------------------------------------------------------------------
# Module-level singletons (shared across all WS handlers)
# ---------------------------------------------------------------------------

conn_tracker = ConnectionTracker()
rate_limiter = TokenBucketLimiter()
