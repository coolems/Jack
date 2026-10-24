"""Multi-instance LLM backend registry (2026-09-08 multi-chat; fleet model 2026-10).

The hand-editable ``config/llama_servers.json`` is the single source of truth for WHICH
llama servers we talk to. This module turns that list into live, tracked backends:

  * :class:`LlmBackend` — one llama-server instance (host:port): its provider handle,
    health state, busy counter, DISCOVERED capacity (``/props.total_slots``), per-slot
    context, loaded model and llama.cpp build. Local instances are spawned by us; remote
    instances (LAN machines) are ATTACH-ONLY — we only probe them and use them while
    healthy. Remote concurrency is owned by the node launcher and discovered here.
  * :class:`LlmBackendRegistry` — the set of all backends with start_all/stop_all, a
    periodic health+props poller, a capacity-aware acquisition helper and a fleet
    snapshot for logs and the fleet table.

Single-instance default: when llama_servers.json holds only the legacy local entry, this
module behaves exactly like the old single-server code path (same process, same port,
one slot).
"""

import asyncio
import logging
import time
from typing import Optional

from config import (
    LlamaServerInstance,
    get_llama_server_instances,
    LLAMA_MAX_CONCURRENCY,
    LLAMA_SERVER_PARALLEL,
    CONTEXT_WINDOW_TOKENS,
)
from .model_state import key_for_api_url
from .model_identity import model_basename, model_key as model_key_of

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

        # --- discovered via /props (2026-10 fleet) --------------------------------
        self.total_slots: Optional[int] = None   # what llama.cpp reports (--parallel N)
        self.ctx_per_slot: Optional[int] = None  # n_ctx of ONE slot
        self.model_key: str = ""                 # canonical routing key (see model_identity)
        self.model_label: str = ""               # display basename
        self.build: str = ""                     # llama.cpp build_info
        self.props_at: float = 0.0
        self.props_error: str = ""
        # Discovery-driven spawn: how many times we relaunched a LOCAL instance to match
        # its reported slot count after booting with the LLAMA_SERVER_PARALLEL fallback.
        # Capped so a pathological /props can never cause a restart loop.
        self._discovery_relaunches = 0
        # One-shot operator warnings (a 30 s poll must not repeat them forever).
        self._warned_slots = False
        self._warned_ctx = False
        self._warned_cap = False
        self._warned_props = False

        # --- error provenance (shown in the fleet table / error frames) ------------
        self.last_error: str = ""
        self.error_count: int = 0

        # Set/cleared by probe_health; workers wait on this instead of polling.
        # NOTE: asyncio.Event binds to the loop that first WAITS on it, not at creation,
        # so creating it on the boot loop and waiting on the serving loop is safe.
        self._healthy_event = asyncio.Event()
        # Serializes "restart this LOCAL node" between the slots' pre-flight/stall paths.
        # Without it, two workers could kill/spawn the same process at the same time.
        self.restart_lock = asyncio.Lock()

    @property
    def label(self) -> str:
        return f"{self.instance.name} ({self.instance.host}:{self.instance.port})"

    # --- capacity ---------------------------------------------------------------

    @property
    def configured_capacity(self) -> Optional[int]:
        """Explicit cap from config (local AND remote). None = DISCOVERED from /props."""
        return self.instance.concurrency

    @property
    def capacity(self) -> int:
        """Effective concurrent-request capacity for routing (discovery-driven).

        * no explicit config value (local or remote): whatever the node reports via
          /props.total_slots - this is the default path, 1 until the first poll lands;
        * explicit config value + not yet discovered: that value (it is also the spawn
          --parallel for local instances);
        * explicit config value AND reported: min(cap, reported) so we never over-admit.

        Never exceeds LLAMA_MAX_CONCURRENCY.
        """
        configured = self.configured_capacity
        reported = self.total_slots if self.total_slots and self.total_slots > 0 else None
        if configured is None:
            cap = reported or 1
        elif reported is None:
            cap = configured
        else:
            cap = min(configured, reported)
        return max(1, min(int(cap), LLAMA_MAX_CONCURRENCY))

    def provider(self):
        """Lazy per-instance LlamaProvider (thin URL-bound wrapper — cheap to create)."""
        if self._provider is None:
            from .provider import LlamaProvider
            from config import PROVIDER_DEFAULT_TIMEOUT
            self._provider = LlamaProvider(self.api_url, PROVIDER_DEFAULT_TIMEOUT)
        return self._provider

    # --- discovery (/props) -------------------------------------------------------

    async def refresh_props(self) -> bool:
        """Read /props: slots, per-slot context, loaded model and llama.cpp build.

        This is how the brain learns what a node actually runs. Failures are recorded
        (props_error) but never mark the backend unhealthy by themselves - /health owns
        that decision; a node may briefly not expose /props while loading.
        """
        from .http_client import get_async_client
        try:
            client = await get_async_client()
            resp = await client.get(f"{self.api_url}/props", timeout=5.0)
            if resp.status_code != 200:
                self.props_error = f"/props HTTP {resp.status_code}"
                return False
            data = resp.json()
        except Exception as e:
            self.props_error = str(e)
            return False

        try:
            slots = data.get("total_slots")
            if isinstance(slots, int) and not isinstance(slots, bool) and slots > 0:
                self.total_slots = slots

            dgs = data.get("default_generation_settings") or {}
            ctx = dgs.get("n_ctx") if isinstance(dgs, dict) else None
            if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
                self.ctx_per_slot = ctx

            raw_model = data.get("model_path") or data.get("model_alias") or ""
            if raw_model:
                self.model_label = model_basename(str(raw_model))
                self.model_key = model_key_of(str(raw_model))
            else:
                # /props without a model means the node is loading or running in router
                # mode. Never keep a previous model key: routing an old-model request
                # here would answer with the wrong model. Treat it as not routable.
                self.props_error = "/props has no model_path"
                return False

            self.build = str(data.get("build_info") or "")
        except Exception as e:  # malformed /props payload - keep whatever we already had
            self.props_error = f"unparseable /props: {e}"
            return False

        self.props_at = time.time()
        self.props_error = ""

        # One-shot sanity warnings: warn when the node runs fewer slots or a smaller
        # context than its config implies. This is the "what do I fix" information for
        # operators, logged once per backend (a 30 s poll must not flood the log).
        if (self.local and self.total_slots is not None and self.configured_capacity is not None
                and self.total_slots < self.configured_capacity and not self._warned_slots):
            self._warned_slots = True
            logger.warning(
                "[REGISTRY] %s reports %d slot(s) but config asks for %d - routing will "
                "use %d (restart the instance to get the configured slots)",
                self.label, self.total_slots, self.configured_capacity, self.capacity,
            )
        if (self.total_slots is not None and self.total_slots > LLAMA_MAX_CONCURRENCY
                and not self._warned_cap):
            self._warned_cap = True
            logger.warning(
                "[REGISTRY] %s reports %d slots - capped at LLAMA_MAX_CONCURRENCY=%d",
                self.label, self.total_slots, LLAMA_MAX_CONCURRENCY,
            )
        if (self.ctx_per_slot is not None and self.ctx_per_slot < CONTEXT_WINDOW_TOKENS
                and not self._warned_ctx):
            self._warned_ctx = True
            logger.warning(
                "[REGISTRY] %s has %d ctx/slot, below CONTEXT_WINDOW_TOKENS=%d - the context "
                "guard will use the node's real value for requests routed here",
                self.label, self.ctx_per_slot, CONTEXT_WINDOW_TOKENS,
            )
        return True

    # --- health ---------------------------------------------------------------

    async def probe_health(self) -> bool:
        """One /health round-trip (+ /props on success). Marks healthy/unhealthy.

        /props is refreshed BEFORE the healthy flag/event is published: workers wake on
        the event and immediately take the next request for ``model_key``, so the model
        key and capacity must already be up to date or they would wait on a stale group.
        """
        from .http_client import get_async_client
        try:
            client = await get_async_client()
            resp = await client.get(f"{self.api_url}/health", timeout=5.0)
            ok = resp.status_code == 200
        except Exception as e:
            logger.debug("[REGISTRY] %s health probe failed: %s", self.label, e)
            ok = False

        previous = bool(self.healthy)
        if ok:
            # Capacity/model are owned by the node: refresh so slots and model changes
            # are picked up automatically (30 s by default). If /props fails we do NOT
            # route here: a stale model_key could send the wrong model to this node.
            props_ok = await self.refresh_props()
            self.healthy = bool(props_ok)
            if not props_ok and not getattr(self, "_warned_props", False):
                self._warned_props = True
                logger.warning(
                    "[REGISTRY] %s answers /health but /props failed (%s) - not routing to it "
                    "until /props works (a stale model key would misroute requests)",
                    self.label, self.props_error or "unknown error",
                )
        else:
            self.healthy = False

        if self.healthy:
            self._healthy_event.set()
        else:
            self._healthy_event.clear()

        if self.healthy != previous:
            state = "HEALTHY" if self.healthy else "UNHEALTHY"
            logger.info("[REGISTRY] Backend %s is now %s%s", self.label, state,
                        "" if self.local else " (remote — attach-only)")
        return bool(self.healthy)

    async def wait_healthy(self, timeout: Optional[float] = None) -> bool:
        """Block until this backend is healthy. Workers call this BEFORE taking work so a
        dead node never consumes a queued request just to fail it."""
        if self.healthy:
            return True
        try:
            await asyncio.wait_for(self._healthy_event.wait(), timeout)
            return bool(self.healthy)
        except asyncio.TimeoutError:
            return False

    def bump_error(self, message: str) -> None:
        """Record a failure against this node (for the fleet table and error frames)."""
        self.last_error = str(message or "")[:500]
        self.error_count += 1

    def mark_unhealthy(self, reason: str = "") -> None:
        """Immediately take this node out of routing (a failed request beats a 30 s poll).

        The health poller re-probes and will bring it back automatically when it recovers.
        """
        if reason:
            self.bump_error(reason)
        if self.healthy:
            logger.warning("[REGISTRY] %s marked UNHEALTHY now: %s", self.label, reason or "request failed")
        self.healthy = False
        self._healthy_event.clear()

    def info(self) -> dict:
        """Compact node identity attached to error/done frames and logs."""
        return {
            "name": self.instance.name,
            "host": self.instance.host,
            "port": self.instance.port,
            "model": self.model_label or self.model_key,
            "model_key": self.model_key,
            "slots": self.capacity,
            "ctx_per_slot": self.ctx_per_slot,
            "build": self.build,
        }

    # --- lifecycle ------------------------------------------------------------

    async def ensure_started(self) -> bool:
        """Make sure this instance is up. Local: spawn + wait for health; remote: probe only.

        (2026-10 discovery model) The SPAWN slot count is the explicit config value when
        present, else LLAMA_SERVER_PARALLEL as a launch fallback - after /props lands the
        DISCOVERED total_slots own routing capacity and the worker pool reconciles to it.

        If no explicit concurrency was configured AND the server reports a different slot
        count than we launched with (e.g. a manually-started llama-server on our port runs
        --parallel 4), relaunch ONCE with the discovered number so process reality matches
        routing capacity - otherwise we would over-admit requests into fewer real slots.

        A MATCHING report never touches the running server: terminate+respawn only happens
        inside the mismatch branch (audit fix 2026-10 - the old structure terminated our own
        tracked process unconditionally and then skipped the respawn, killing llama at boot
        while still reporting HEALTHY).
        """
        from .server import start_server
        if not self.local:
            return await self.probe_health()

        launch_slots = self.configured_capacity or LLAMA_SERVER_PARALLEL
        ok = await start_server(
            self.api_url,
            host=self.instance.host,
            port=self.instance.port,
            concurrency=launch_slots,
        )
        if not ok:
            self.healthy = False
            return False

        # Same ordering rule as probe_health: discovery BEFORE workers can wake.
        props_ok = await self.refresh_props()

        # Discovery-driven relaunch (only when the operator gave no explicit value AND the
        # server's real slot count differs from what we launched with). The terminate+respawn
        # happens ONLY inside that branch: a matching report must never kill a healthy server.
        reported = self.total_slots if isinstance(self.total_slots, int) and self.total_slots > 0 else None
        need_relaunch = bool(
            props_ok and self.configured_capacity is None and reported is not None
            and reported != launch_slots
        )

        if need_relaunch:
            if self._discovery_relaunches < 2:
                # NOTE: start_server() fast-paths "we already own a tracked process", so we
                # must terminate our OWN process first to force a real respawn with the new
                # --parallel. Under restart_lock so a worker-side pre-flight/stall restart of
                # this node can never interleave with this relaunch (see streaming.py).
                from .process_mgmt import (_server_processes, _terminate_process,
                                           wait_for_port_free_async)
                async with self.restart_lock:
                    logger.info(
                        "[REGISTRY] %s reports %d slot(s) but was launched with --parallel %d "
                        "(no explicit 'concurrency' in config) - relaunching with the discovered count",
                        self.label, reported, launch_slots,
                    )
                    _proc = _server_processes.get(self.key)
                    if _proc is not None and _proc.poll() is None:
                        logger.info("[REGISTRY] %s terminating our tracked llama-server (PID %d) "
                                    "for a discovery relaunch", self.label, _proc.pid)
                        await asyncio.to_thread(_terminate_process, _proc)
                    # Wait for the port to actually free: start_server()'s fast path would
                    # otherwise attach to whatever still answers /health instead of respawning.
                    if not await wait_for_port_free_async(self.instance.port):
                        logger.warning("[REGISTRY] %s port %d still busy after terminate - "
                                       "marking node DOWN; health poller will retry",
                                       self.label, self.instance.port)
                        ok = False   # we killed our own server and the port is still taken
                    else:
                        self._discovery_relaunches += 1
                        ok = await start_server(
                            self.api_url,
                            host=self.instance.host,
                            port=self.instance.port,
                            concurrency=reported,
                        )
                if not ok:
                    logger.warning("[REGISTRY] %s relaunch with discovered slots failed - "
                                   "node is DOWN now; health poller will retry", self.label)
            else:
                logger.warning(
                    "[REGISTRY] %s still reports %d slot(s) after %d discovery relaunch(es) - "
                    "stopping automatic relaunches; routing capacity follows /props",
                    self.label, reported, self._discovery_relaunches,
                )

        self.healthy = bool(props_ok and ok)
        if self.healthy:
            self._healthy_event.set()
        else:
            self._healthy_event.clear()
        return bool(self.healthy)

    def acquire(self) -> None:
        """Mark one request as running on this backend (capacity accounting)."""
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
        """First configured backend (legacy single-instance code paths point at it)."""
        return next(iter(self.backends.values()))

    def get_by_key(self, key: tuple[str, int]) -> Optional[LlmBackend]:
        return self.backends.get(key)

    def get_for_api_url(self, api_url: str) -> Optional[LlmBackend]:
        k = key_for_api_url(api_url)
        return self.backends.get(k) if k else None

    def healthy_backends(self) -> list[LlmBackend]:
        return [b for b in self.backends.values() if b.healthy]

    def backends_for_model(self, model: str) -> list[LlmBackend]:
        """Backends whose discovered model matches *model* (healthy or not)."""
        want = model_key_of(model)
        if not want:
            return []
        return [b for b in self.backends.values() if b.model_key == want]

    def model_configured(self, model: str) -> bool:
        """True when at least one node is CONFIGURED for this model (healthy or not)."""
        return bool(self.backends_for_model(model))

    def model_healthy(self, model: str) -> bool:
        """True when at least one HEALTHY node serves this model."""
        return any(b.healthy for b in self.backends_for_model(model))

    def describe_models(self) -> str:
        """Human-readable model groups for 'no node serves X' errors and logs."""
        groups: dict[str, dict] = {}
        for b in self.backends.values():
            key = b.model_key or "?"
            g = groups.setdefault(key, {"nodes": 0, "healthy": 0, "slots": 0})
            g["nodes"] += 1
            g["slots"] += b.capacity
            if b.healthy:
                g["healthy"] += 1
        if not groups:
            return "none"
        return ", ".join(
            f"{key} ({g['healthy']}/{g['nodes']} node(s) up, {g['slots']} slot(s))"
            for key, g in sorted(groups.items())
        )

    def acquire_backend(self, model: str = "") -> Optional[LlmBackend]:
        """Least-busy HEALTHY backend WITH A FREE SLOT (FIFO tie-break by config order).

        Kept as the capacity-aware acquisition helper; the worker pool uses one worker
        per slot, so this is mainly used by diagnostics/future routing code.
        """
        want = model_key_of(model) if model else ""
        candidates = [
            b for b in self.backends.values()
            if b.healthy and b.busy < b.capacity and (not want or b.model_key == want)
        ]
        if not candidates:
            return None
        order = list(self.backends.values())
        pick = min(candidates, key=lambda b: (b.busy / max(1, b.capacity), order.index(b)))
        pick.acquire()
        return pick

    def total_slots(self) -> int:
        return sum(b.capacity for b in self.backends.values() if b.healthy)

    def effective_context_window(self) -> int:
        """Smallest healthy per-slot context (the client trims prompts against this).

        With mixed machines, using the minimum is the only safe global number: a prompt
        sized for 181k could overflow a node that only has 64k per slot.
        """
        vals = [b.ctx_per_slot for b in self.backends.values()
                if b.healthy and b.ctx_per_slot and b.ctx_per_slot > 0]
        return min(vals) if vals else CONTEXT_WINDOW_TOKENS

    def snapshot(self) -> list[dict]:
        """Per-backend state for the fleet table / logs / error frames."""
        return [
            {
                "name": b.instance.name,
                "host": b.instance.host,
                "port": b.instance.port,
                "local": b.local,
                "healthy": bool(b.healthy),
                "configured": b.configured_capacity,
                "slots": b.capacity,
                "reported_slots": b.total_slots,
                "ctx_per_slot": b.ctx_per_slot,
                "model": b.model_label,
                "model_key": b.model_key,
                "build": b.build,
                "busy": b.busy,
                "error": b.props_error or b.last_error,
                "error_count": b.error_count,
            }
            for b in self.backends.values()
        ]

    # --- lifecycle ------------------------------------------------------------

    async def _probe_one(self, backend: LlmBackend) -> bool:
        try:
            return await backend.probe_health()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("[REGISTRY] probe error for %s: %s", backend.label, e)
            return False

    async def probe_all(self) -> list[tuple[LlmBackend, bool]]:
        """Probe /health + /props for every backend, IN PARALLEL.

        A fleet of down nodes would otherwise cost 5 s each (connect timeout) before the
        server can accept its first client.
        """
        backends = list(self.backends.values())
        results = await asyncio.gather(*(self._probe_one(b) for b in backends))
        return list(zip(backends, results))

    def log_fleet_table(self, title: str = "LLAMA FLEET") -> None:
        """Log the startup/refresh table (model, slots, ctx/slot, build)."""
        rows = self.snapshot()
        logger.info("=" * 78)
        logger.info("%s (%d node(s), %d healthy slot(s) total):",
                    title, len(rows), self.total_slots())
        for r in rows:
            if r["healthy"]:
                state = "READY "
            elif r["healthy"] is False:
                state = "DOWN  "
            else:
                state = "?     "
            ctx = f"{r['ctx_per_slot']}" if r["ctx_per_slot"] else "?"
            slots = f"{r['slots']}/{r['reported_slots'] or r['configured'] or '?'}"
            logger.info(
                "  %-12s %-21s %s slots %-7s ctx/slot %-7s %-28s %s%s",
                r["name"], f"{r['host']}:{r['port']}", state, slots, ctx,
                (r["model"] or "(unknown model)"), (r["build"] or ""),
                f"  busy={r['busy']}" if r["healthy"] else
                (f"  err={r['error'][:60]}" if r["error"] else ""),
            )
        logger.info("=" * 78)

    async def start_all(self) -> bool:
        """Start/attach every configured instance and log the startup fleet table.

        Returns True when at least one backend is healthy (the SERVER can serve)."""
        results = []
        for b in self.backends.values():
            ok = await b.ensure_started()
            results.append((b, ok))

        self.log_fleet_table("LLAMA BACKEND REGISTRY (from config/llama_servers.json)")

        any_ok = any(ok for _, ok in results)
        if not any_ok:
            logger.error("[REGISTRY] No llama backend is available - chat requests will fail "
                         "until one becomes healthy")
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
        """Background task re-probing every backend (and refreshing its /props).

        (2026-10 fleet fix) Safe to call again on a DIFFERENT event loop: the SERVER boots
        on one temporary asyncio.run() loop and then serves on another, which used to leave
        this poller dead after boot (health + capacity frozen forever). A task owned by a
        different/closed loop is replaced here.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop, nothing we can schedule
            logger.warning("[REGISTRY] start_health_poller called without a running loop")
            return None

        task = getattr(self, "_poller_task", None)
        if task is not None and not task.done() and task.get_loop() is running:
            return task  # already polling on this loop
        if task is not None and not task.done():
            logger.info("[REGISTRY] Replacing health poller bound to a different event loop")
            task.cancel()

        async def _poll_loop():
            while True:
                await asyncio.sleep(interval_sec)
                # Parallel probes: one dead/slow node must not delay the others.
                await asyncio.gather(
                    *(self._probe_one(b) for b in list(self.backends.values()))
                )

        self._poller_task = asyncio.create_task(_poll_loop(), name="llama-registry-health-poller")
        logger.info("[REGISTRY] Health poller started (every %ds)", interval_sec)
        return self._poller_task

    def stop_health_poller(self) -> None:
        task = getattr(self, "_poller_task", None)
        if task is not None and not task.done():
            task.cancel()

    # (2026-10 fleet) Model switching was REMOVED: each node is started with a fixed
    # model, and requests are routed to the model group they name. Nothing reloads llama
    # during serving.


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
