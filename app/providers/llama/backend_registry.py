"""Multi-instance LLM backend registry (2026-09-08 multi-chat; fleet model 2026-10).

The hand-editable ``config/llama_servers.json`` is the single source of truth for WHICH
llama servers we talk to and WHAT each one runs. This module turns that list into live,
tracked backends:

  * :class:`LlmBackend` — one backend entry (host:port): its provider handle, health state,
    busy counter, DISCOVERED capacity (``/props.total_slots``), per-slot context, loaded
    model and llama.cpp build. LOCAL entries are reconciled at boot by the fleet manager
    (``node_launcher.reconcile``): attach to a healthy node that serves this entry's model,
    otherwise spawn one with THIS entry's settings — after the memory gate says it fits.
    Remote entries (LAN machines) are ATTACH-ONLY.

  * Shutdown semantics (2026-10-25): ``stop_all()`` terminates ONLY backends WE spawned
    (``spawned_by_us=True``, tracked Popen). A node started outside us (admin launcher,
    manual) is attached but NEVER killed on SERVER exit.
  * :class:`LlmBackendRegistry` — the set of all backends with start_all/stop_all, a
    periodic health+props poller, a capacity-aware acquisition helper and a fleet
    snapshot for logs and the fleet table.

Single-entry default: when llama_servers.json holds only the local entry, this module
behaves exactly like the old single-server code path (same process, same port, one slot).

(2026-11 strata backend) The same registry also drives ``config/strata_servers.json`` when
config.SERVER_BACKEND = "strata": those entries carry ``kind="strata"`` and a Bearer key,
are ALWAYS attach-only (``spawnable=False`` - Strata starts its own process, we never spawn
or kill it), their /health + /props + /v1/models calls carry the auth header, and their
provider handle is a :class:`StrataProvider` instead of a :class:`LlamaProvider`.
"""

import asyncio
import logging
import time
from typing import Optional

from config import (
    LlamaServerInstance,
    get_llama_server_instances,
    get_strata_server_instances,
    strata_auth_headers,
    LLAMA_MAX_CONCURRENCY,
    CONTEXT_WINDOW_TOKENS,
)
from .model_state import key_for_api_url
from .model_identity import model_basename, model_key as model_key_of

logger = logging.getLogger("COOLEMS.Provider.Llama.Registry")


def _is_local(host: str) -> bool:
    """Local instances are the ones we may spawn processes for."""
    return host.lower() in ("127.0.0.1", "localhost", "::1")


class LlmBackend:
    """One backend from config/llama_servers.json or config/strata_servers.json."""

    def __init__(self, instance: LlamaServerInstance):
        self.instance = instance
        self.key: tuple[str, int] = (instance.host.lower(), instance.port)
        self.api_url: str = instance.api_url
        self.local: bool = _is_local(instance.host)
        # (2026-11 strata) Backend kind: "llama" for llama-server, "strata" for a Strata
        # endpoint. Strata instances subclass LlamaServerInstance with api_key + kind and
        # are attach-only, so the brain only probes them and authenticates every request.
        self.kind: str = getattr(instance, "kind", "llama")
        self.api_key: Optional[str] = getattr(instance, "api_key", None)
        self.headers: Optional[dict] = strata_auth_headers(self.api_key)
        # LOCAL llama nodes may be spawned by us; everything else (remotes, Strata) is
        # ATTACH-ONLY: a failed probe must never start or restart a process we do not own.
        self.spawnable: bool = self.local and self.kind == "llama"
        # (2026-10-25) True only when WE launched this instance's llama-server at boot
        # (tracked Popen in process_mgmt). Attached foreign nodes keep False so stop_all()
        # never kills a node an admin started.
        self.spawned_by_us: bool = False
        self.healthy: Optional[bool] = None      # None = not probed yet
        self.busy: int = 0                       # requests currently running on this backend
        self._provider = None                    # lazy LlamaProvider handle

        # --- discovered via /props (2026-10 fleet) --------------------------------
        self.total_slots: Optional[int] = None   # what llama.cpp reports (--parallel N)
        # n_ctx of ONE slot. A configured value on the entry ('ctx_per_slot') is the
        # starting point; /props overwrites it with what the node actually reports.
        self.ctx_per_slot: Optional[int] = getattr(instance, "ctx_per_slot", None)
        self.model_key: str = ""                 # canonical routing key (see model_identity)
        # (2026-11 strata) Extra canonical keys this backend must route: an endpoint can
        # name the same model under an alias id (/props.model_alias, /v1/models ids,
        # llama's router aliases). Clients pick those ids from the catalog.
        self.model_aliases: set[str] = set()
        self.model_label: str = ""               # display basename
        self.build: str = ""                     # llama.cpp build_info
        self.props_at: float = 0.0
        self.props_error: str = ""
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
        """Lazy per-instance provider handle (thin URL-bound wrapper — cheap to create).

        Kind decides the class: llama nodes get LlamaProvider, Strata endpoints get
        StrataProvider (same OpenAI-compatible wire format + Bearer auth headers)."""
        if self._provider is None:
            from config import PROVIDER_DEFAULT_TIMEOUT
            if self.kind == "strata":
                from ..strata.provider import StrataProvider
                self._provider = StrataProvider(self.api_url, PROVIDER_DEFAULT_TIMEOUT,
                                                headers=self.headers)
            else:
                from .provider import LlamaProvider
                self._provider = LlamaProvider(self.api_url, PROVIDER_DEFAULT_TIMEOUT)
        return self._provider

    def serves_model(self, model_key: str) -> bool:
        """True when requests for this canonical model key may run on this backend.

        (2026-11 strata) One endpoint serves one model under SEVERAL ids: ``/props`` names
        the GGUF file, ``/props.model_alias`` and ``/v1/models`` report the alias id the
        CLIENT picker shows and sends. Both must route to the same slot group, otherwise
        the worker's routing guard rejects a request for the very model this backend
        serves - and with ``parallel`` slots that is every request, not just one.
        """
        if not model_key:
            return False
        key = model_key_of(model_key)
        return key == self.model_key or key in self.model_aliases

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
            resp = await client.get(f"{self.api_url}/props", timeout=5.0, headers=self.headers)
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
                # (2026-11 strata) Every other id this endpoint answers for the SAME model
                # becomes a routable key. The canonical key stays the model_path basename so
                # profile authorization (GGUF filenames) keeps matching the display name.
                aliases: set[str] = set()
                for raw in (data.get("model_alias"), *(data.get("aliases") or [])):
                    if isinstance(raw, str) and raw:
                        k = model_key_of(raw)
                        if k and k != self.model_key:
                            aliases.add(k)
                self.model_aliases = aliases
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
            resp = await client.get(f"{self.api_url}/health", timeout=5.0, headers=self.headers)
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
                _hint = ("" if self.headers or self.kind != "strata" else
                         " - this Strata endpoint has NO Bearer key (set 'api_key' in "
                         "config/strata_servers.json or config.STRATA_API_KEY)")
                logger.warning(
                    "[REGISTRY] %s answers /health but /props failed (%s)%s - not routing to it "
                    "until /props works (a stale model key would misroute requests)",
                    self.label, self.props_error or "unknown error", _hint,
                )
        else:
            self.healthy = False

        if self.healthy:
            self._healthy_event.set()
        else:
            self._healthy_event.clear()

        if self.healthy != previous:
            state = "HEALTHY" if self.healthy else "UNHEALTHY"
            note = "" if self.spawnable else (
                " (remote — attach-only)" if self.kind == "llama" else " (Strata — attach-only)")
            logger.info("[REGISTRY] Backend %s is now %s%s", self.label, state, note)
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
            "kind": self.kind,
            "model": self.model_label or self.model_key,
            "model_key": self.model_key,
            "model_aliases": sorted(self.model_aliases),
            "slots": self.capacity,
            "ctx_per_slot": self.ctx_per_slot,
            "build": self.build,
        }

    # --- lifecycle ------------------------------------------------------------

    async def ensure_started(self) -> bool:
        """Make sure this instance is up. Local llama: reconcile (attach or spawn); else probe only.

        (2026-11 multi-config fleet) The entry in config/llama_servers.json IS the config:
        its model, slots, ctx/slot, threads, gpu layers, bind and extra flags are what a
        spawn uses — there is no separate per-machine file and no "reconcile against
        defaults" step. node_launcher.reconcile() does the whole decision:

          * healthy node on our port serving THIS entry's model -> ATTACH (spawned_by_us
            stays False; it survives SERVER shutdown)
          * port occupied by a different model / a non-llama process -> REPORT, never kill
          * nothing on the port and auto_start=true -> memory gate, then spawn detached
          * auto_start=false -> probe only (an admin owns the lifecycle)

        The old discovery-driven relaunch is gone: routing capacity always follows
        /props.total_slots, and we never kill a process we did not start to "fix" it.

        (2026-11 strata) ATTACH-ONLY backends (`spawnable=False`: remote llama nodes and
        every Strata endpoint) are probe-only - their processes belong to their owners.
        """
        if not self.spawnable:
            return await self.probe_health()

        from . import node_launcher
        try:
            spawned, ok = await node_launcher.reconcile(self.instance)
        except Exception as e:  # pragma: no cover - defensive (reconcile never raises)
            logger.error("[REGISTRY] %s reconcile failed: %s", self.label, e)
            spawned, ok = False, False
        self.spawned_by_us = bool(spawned and ok)
        if not ok:
            self.healthy = False
            self._healthy_event.clear()
            return False

        # Same ordering rule as probe_health: discovery BEFORE workers can wake.
        props_ok = await self.refresh_props()
        self.healthy = bool(props_ok)
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
    """All configured backends, started together and health-polled.

    (2026-11) ``from_config(kind="strata")`` builds the same registry over
    config/strata_servers.json instead of config/llama_servers.json - every backend then
    reports ``kind="strata"`` and is attach-only."""

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
    def from_config(cls, kind: str = "llama") -> "LlmBackendRegistry":
        """Build the registry from a fresh read of the backend's config file.

        ``kind`` selects the source of truth: "llama" -> config/llama_servers.json
        (spawn-or-attach llama-server instances), "strata" -> config/strata_servers.json
        (attach-only Strata endpoints)."""
        if kind == "strata":
            return cls(get_strata_server_instances())
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
        """Backends whose discovered model matches *model* (healthy or not).

        (2026-11 strata) Matches the canonical key AND the endpoint's alias ids, so a
        client that picked the alias from ``/v1/models`` lands in this backend's queue."""
        want = model_key_of(model)
        if not want:
            return []
        return [b for b in self.backends.values() if b.serves_model(want)]

    def model_configured(self, model: str) -> bool:
        """True when at least one node is CONFIGURED for this model (healthy or not)."""
        return bool(self.backends_for_model(model))

    def canonical_model_key(self, model: str) -> str:
        """The queue key a request for *model* must use.

        (2026-11 strata) Workers poll ONE queue per backend - the backend's canonical
        ``model_key``. A client may name the model by an alias id (``/v1/models``), whose
        key would land in a FIFO no worker reads, so the request is re-keyed to the group
        that serves it. Falls back to the caller's key when no backend claims the model
        (the "no node serves this" path then reports it as asked)."""
        want = model_key_of(model)
        if not want:
            return ""
        for b in self.backends.values():
            if b.serves_model(want):
                return b.model_key
        return want

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
            if b.healthy and b.busy < b.capacity and (not want or b.serves_model(want))
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
                "kind": b.kind,
                "local": b.local,
                "healthy": bool(b.healthy),
                "configured": b.configured_capacity,
                "slots": b.capacity,
                "reported_slots": b.total_slots,
                "ctx_per_slot": b.ctx_per_slot,
                "model": b.model_label,
                "model_key": b.model_key,
                "model_aliases": sorted(b.model_aliases),
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

    def registry_title(self) -> str:
        """Operator-facing registry title naming the config file the backends came from."""
        kinds = {b.kind for b in self.backends.values()}
        if kinds == {"strata"}:
            return "STRATA BACKEND REGISTRY (from config/strata_servers.json)"
        if kinds == {"llama"}:
            return "LLAMA BACKEND REGISTRY (from config/llama_servers.json)"
        return "LLM BACKEND REGISTRY (mixed: " + ", ".join(sorted(kinds)) + ")"

    def backend_noun(self) -> str:
        """Client-facing wording for a serving backend, by kind (2026-11 strata).

        User messages must not say "llama node" when the fleet is Strata — the fix a
        user needs is different (start Strata / set its key, not launch a llama node)."""
        kinds = {b.kind for b in self.backends.values()}
        if kinds == {"strata"}:
            return "Strata endpoint"
        if kinds == {"llama"}:
            return "llama node"
        return "backend"

    async def start_all(self) -> bool:
        """Start/attach every configured instance and log the startup fleet table.

        Returns True when at least one backend is healthy (the SERVER can serve)."""
        # (2026-11 memory gate) One boot line about what the machine actually has, so an
        # operator can read WHY a LOCAL entry was refused (free RAM/VRAM vs what a launch
        # needs) without guessing. Never raises - an unreadable machine reports zeros.
        if any(b.spawnable for b in self.backends.values()):
            try:
                from . import memory
                logger.info("[REGISTRY] machine memory now: %s", memory.snapshot().describe())
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("[REGISTRY] memory snapshot unavailable: %s", e)

        results = []
        for b in self.backends.values():
            ok = await b.ensure_started()
            results.append((b, ok))

        self.log_fleet_table(self.registry_title())

        any_ok = any(ok for _, ok in results)
        if not any_ok:
            logger.error("[REGISTRY] No llama backend is available - chat requests will fail "
                         "until one becomes healthy")
        return any_ok

    async def stop_all(self) -> None:
        """Stop the instances WE spawned. Remotes and ATTACHED nodes are left running.

        (2026-10-25 shutdown semantics) A node started outside us (admin launcher via
        utils/start_llama_node, or a manual llama-server we merely attached to) is NOT
        terminated on SERVER exit - it survives for other users of the machine. Only
        backends with spawned_by_us=True and a live tracked Popen are stopped.

        (2026-11 strata) Attach-only backends (`spawnable=False`: remotes and every Strata
        endpoint) are skipped outright - Strata's process belongs to Strata, and killing it
        on SERVER exit would take the local inference endpoint down for everything else."""
        from .process_mgmt import _server_processes, _terminate_process
        for b in self.backends.values():
            if not b.spawnable:
                continue
            proc = _server_processes.get(b.key)
            if proc is None or proc.poll() is not None:
                continue
            if not b.spawned_by_us:
                logger.info("[REGISTRY] Leaving attached local node %s (PID %d) RUNNING - "
                            "the SERVER did not spawn it", b.label, proc.pid)
                continue
            logger.info("[REGISTRY] Stopping local backend %s we spawned (PID %d)", b.label, proc.pid)
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
