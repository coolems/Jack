"""Multi-instance LLM backend registry (2026-09-08 multi-chat).

The hand-editable ``config/llama_servers.json`` is the single source of truth for WHICH
llama servers we create. This module turns that list into live, tracked backends:

  * :class:`LlmBackend` — one llama-server instance (host:port): its provider handle,
    health state and busy counter. Local instances are spawned by us; remote instances
    (LAN machines) are ATTACH-ONLY — we only probe them and use them while healthy.
  * :class:`LlmBackendRegistry` — the set of all backends with start_all/stop_all, a
    periodic health poller, least-busy acquisition for the worker pool, and
    switch_model() which reloads every local instance (all run the same model) and
    verifies remotes afterwards.

Single-instance default: when llama_servers.json holds only the legacy local entry, this
module behaves exactly like the old single-server code path (same process, same port).
"""

import asyncio
import logging
from typing import Optional

from config import LlamaServerInstance, get_llama_server_instances
from .model_state import key_for_api_url

logger = logging.getLogger("COOLEMS.Provider.Llama.Registry")


def _is_local(host: str) -> bool:
    """Local instances are the ones we may spawn processes for."""
    return host.lower() in ("127.0.0.1", "localhost", "::1")


class LlmBackend:
    """One llama-server instance from config/llama_servers.json."""

    def __init__(self, instance: LlamaServerInstance):
        self.instance = instance
        self.key: tuple[str, int] = (instance.host.lower(), instance.port)
        self.api_url: str = instance.api_url
        self.local: bool = _is_local(instance.host)
        self.healthy: Optional[bool] = None      # None = not probed yet
        self.busy: int = 0                       # requests currently running on this backend
        self._provider = None                    # lazy LlamaProvider handle

    @property
    def label(self) -> str:
        return f"{self.instance.name} ({self.instance.host}:{self.instance.port})"

    def provider(self):
        """Lazy per-instance LlamaProvider (thin URL-bound wrapper — cheap to create)."""
        if self._provider is None:
            from .provider import LlamaProvider
            from config import PROVIDER_DEFAULT_TIMEOUT
            self._provider = LlamaProvider(self.api_url, PROVIDER_DEFAULT_TIMEOUT)
        return self._provider

    # --- health ---------------------------------------------------------------

    async def probe_health(self) -> bool:
        """One /health round-trip. Marks healthy/unhealthy and logs transitions."""
        from .http_client import get_async_client
        try:
            client = await get_async_client()
            resp = await client.get(f"{self.api_url}/health", timeout=5.0)
            ok = resp.status_code == 200
        except Exception as e:
            logger.debug("[REGISTRY] %s health probe failed: %s", self.label, e)
            ok = False

        if ok != (self.healthy or False):
            state = "HEALTHY" if ok else "UNHEALTHY"
            logger.info("[REGISTRY] Backend %s is now %s%s", self.label, state,
                        "" if self.local else " (remote — attach-only)")
        self.healthy = ok
        return ok

    # --- lifecycle ------------------------------------------------------------

    async def ensure_started(self) -> bool:
        """Make sure this instance is up. Local: spawn + wait for health; remote: probe only."""
        from .server import start_server
        if not self.local:
            return await self.probe_health()
        ok = await start_server(self.api_url, host=self.instance.host, port=self.instance.port)
        if ok:
            self.healthy = True
        else:
            self.healthy = False
        return ok

    def acquire(self) -> None:
        """Mark one request as running on this backend (least-busy selection)."""
        self.busy += 1

    def release(self) -> None:
        if self.busy > 0:
            self.busy -= 1


class LlmBackendRegistry:
    """All configured llama backends, started together and health-polled."""

    def __init__(self, instances: list[LlamaServerInstance]):
        # de-dup by key (loader already dedupes; defensive for direct construction)
        seen = set()
        self.backends: dict[tuple[str, int], LlmBackend] = {}
        # Model-switch drain policy: while True, workers reject NEW requests with a clear frame.
        self.switch_in_progress: bool = False
        for inst in instances:
            b = LlmBackend(inst)
            if b.key not in seen:
                seen.add(b.key)
                self.backends[b.key] = b

    @classmethod
    def from_config(cls) -> "LlmBackendRegistry":
        """Build the registry from a fresh read of config/llama_servers.json."""
        return cls(get_llama_server_instances())

    # --- accessors ------------------------------------------------------------

    def primary(self) -> LlmBackend:
        """First configured backend (the one legacy single-instance code paths point at)."""
        return next(iter(self.backends.values()))

    def get_by_key(self, key: tuple[str, int]) -> Optional[LlmBackend]:
        return self.backends.get(key)

    def get_for_api_url(self, api_url: str) -> Optional[LlmBackend]:
        k = key_for_api_url(api_url)
        return self.backends.get(k) if k else None

    def healthy_backends(self) -> list[LlmBackend]:
        return [b for b in self.backends.values() if b.healthy]

    def acquire_backend(self, model: str = "") -> Optional[LlmBackend]:
        """Least-busy HEALTHY backend (FIFO tie-break by config order). None when all busy/unhealthy."""
        candidates = [b for b in self.backends.values() if b.healthy and b.busy == 0]
        if not candidates:
            return None
        pick = min(candidates, key=lambda b: list(self.backends.values()).index(b))
        pick.acquire()
        return pick

    # --- lifecycle ------------------------------------------------------------

    async def start_all(self) -> bool:
        """Start/attach every configured instance and log the startup table.

        Returns True when at least one backend is healthy (the SERVER can serve)."""
        results = []
        for b in self.backends.values():
            ok = await b.ensure_started()
            results.append((b, ok))

        logger.info("=" * 62)
        logger.info("LLAMA BACKEND REGISTRY (%d instance(s) from config/llama_servers.json):", len(results))
        for b, ok in results:
            state = "READY" if ok else ("UNAVAILABLE (remote)" if not b.local else "FAILED TO START")
            logger.info("  %-28s %s:%-6d %s", b.instance.name, b.instance.host, b.instance.port, state)
        logger.info("=" * 62)

        any_ok = any(ok for _, ok in results)
        if not any_ok:
            logger.error("[REGISTRY] No llama backend is available - chat requests will fail until one becomes healthy")
        return any_ok

    async def stop_all(self) -> None:
        """Stop all locally-spawned instances (tracked processes). Remotes are left alone."""
        from .process_mgmt import _server_processes, _terminate_process
        for b in self.backends.values():
            if not b.local:
                continue
            proc = _server_processes.get(b.key)
            if proc is not None and proc.poll() is None:
                logger.info("[REGISTRY] Stopping local backend %s (PID %d)", b.label, proc.pid)
                await asyncio.to_thread(_terminate_process, proc)

    # --- health poller ----------------------------------------------------------

    def start_health_poller(self, interval_sec: int = 30) -> Optional[asyncio.Task]:
        """Background task re-probing every backend; unhealthy ones are simply not picked."""
        async def _poll_loop():
            while True:
                await asyncio.sleep(interval_sec)
                for b in list(self.backends.values()):
                    try:
                        await b.probe_health()
                    except Exception as e:  # pragma: no cover - defensive
                        logger.debug("[REGISTRY] poll error for %s: %s", b.label, e)

        task = asyncio.create_task(_poll_loop(), name="llama-registry-health-poller")
        self._poller_task = task
        return task

    def stop_health_poller(self) -> None:
        task = getattr(self, "_poller_task", None)
        if task is not None and not task.done():
            task.cancel()

    # --- model switching --------------------------------------------------------

    async def switch_model(self, model_filename: str):
        """Public entry point with the drain-policy flag (see *switch_in_progress*).

        While this runs, queued NEW requests are rejected by the worker pool with a clear
        'model switch in progress' frame; already-running ones finish on their backend.
        The flag is cleared in `finally` so an exception can never wedge it."""
        self.switch_in_progress = True
        try:
            return await self._switch_model_impl(model_filename)
        finally:
            self.switch_in_progress = False

    async def _switch_model_impl(self, model_filename: str):
        """Switch ALL local instances to *model_filename* (they all run the same model),
        then verify remotes. Returns (success, message).

        Sequential reloads on purpose: each instance owns its VRAM; parallel cold loads of a
        large model would OOM. Remotes are verified afterwards and marked unhealthy on
        mismatch (the operator must switch them by hand — we cannot spawn there)."""
        from .server import reload_with_model, get_current_model

        local = [b for b in self.backends.values() if b.local]
        remote = [b for b in self.backends.values() if not b.local]

        # Re-entrancy: the legacy single-instance guard lives inside server.reload_with_model;
        # with several locals we serialize them here and rely on that guard per instance.
        errors = []
        for b in local:
            ok, msg = await reload_with_model(b.api_url, model_filename, host=b.instance.host, port=b.instance.port)
            if not ok:
                errors.append(f"{b.label}: {msg}")

        remote_mismatch = []
        for b in remote:
            current = await get_current_model(b.api_url, force=True) or ""
            cur_base = current.rsplit("/", 1)[-1].lower()
            want_base = model_filename.lower()
            if cur_base != want_base and not (cur_base.endswith(".gguf") and cur_base[:-5] == want_base):
                remote_mismatch.append(b.label)

        if errors:
            return False, "Model switch failed on local instance(s): " + "; ".join(errors)
        if remote_mismatch:
            logger.warning("[REGISTRY] Remote instance(s) still run a different model after switch (marked unhealthy): %s",
                           ", ".join(remote_mismatch))
            for label in remote_mismatch:
                for b in self.backends.values():
                    if b.label == label:
                        b.healthy = False
        return True, f"Model switched to {model_filename} on {len(local)} local instance(s)"


# ---------------------------------------------------------------------------
# Module-level singleton (created at boot by the provider; None until then)
# ---------------------------------------------------------------------------

_registry: Optional[LlmBackendRegistry] = None


def set_registry(registry: LlmBackendRegistry) -> None:
    """Register the live registry (called once during SERVER boot)."""
    global _registry
    _registry = registry


def get_registry() -> Optional[LlmBackendRegistry]:
    return _registry
