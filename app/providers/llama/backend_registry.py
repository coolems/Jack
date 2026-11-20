"""Multi-instance LLM backend registry.

config/llama_servers.json decides WHICH llama servers we talk to; this module turns that
list into live, tracked backends:
  * LlmBackend - one instance (host:port): provider handle, health, busy counter,
    discovered capacity (/props.total_slots), per-slot context, loaded model, llama.cpp
    build and its ThinkingPolicy. LOCAL instances are reconciled at boot by the node
    launcher (attach to a healthy matching node, else spawn from config/llama_node.json);
    remote instances are attach-only.
  * LlmBackendRegistry - start_all/stop_all, a periodic health+props poller, a
    capacity-aware acquisition helper and a snapshot for logs and the table. stop_all
    terminates only backends we spawned (spawned_by_us=True with a tracked Popen).
"""

import asyncio
import logging
import time
from typing import Optional

from config import (
    LlamaServerInstance,
    get_llama_server_instances,
    get_strata_server_instances,
    get_deepseek_server_instances,
    strata_auth_headers,
    deepseek_auth_headers,
    LLAMA_MAX_CONCURRENCY,
    LLAMA_SERVER_PARALLEL,
    CONTEXT_WINDOW_TOKENS,
)
from .model_state import key_for_api_url
from .model_identity import model_basename, model_key as model_key_of
from .thinking import ThinkingPolicy

logger = logging.getLogger("COOLEMS.Provider.Llama.Registry")


def _is_local(host: str) -> bool:
    """Local instances are the ones we may spawn processes for."""
    return host.lower() in ("127.0.0.1", "localhost", "::1")


def _deepseek_child_instance(endpoint_instance, model_key: str, ctx):
    """The instance of one DeepSeek model's routable child backend.

    Same endpoint identity (host/port/base_url/api_key/thinking override) plus the model id,
    so the child's api_url still resolves to the endpoint and its key is the model's queue key.
    """
    from config import DeepSeekServerInstance

    return DeepSeekServerInstance(
        f"{endpoint_instance.name}:{model_key}",
        endpoint_instance.host, endpoint_instance.port,
        endpoint_instance.concurrency, endpoint_instance.thinking,
        api_key=endpoint_instance.api_key, ctx_per_slot=ctx,
        base_url=endpoint_instance.base_url, model_id=model_key,
    )


def _startup_reasoning_enabled() -> bool:
    """True when a llama node we spawn is started with --reasoning.

    That flag is startup-only: llama.cpp separates reasoning into reasoning_content even for
    a template with no request knob, so the UI can display thinking but no request can turn
    it off. Read lazily so a test or CLI override of config is honored.
    """
    try:
        import config as _cfg
        return bool(getattr(_cfg, "LLAMA_SERVER_REASONING", False))
    except Exception:  # pragma: no cover - defensive
        return False


class LlmBackend:
    """One backend from config/llama_servers.json, config/strata_servers.json or
    config/deepseek_servers.json.

    A DeepSeek entry is an ENDPOINT (one hosted base_url). Because one endpoint serves
    several model ids, the registry expands it into one routable child backend per model
    (registry.sync_deepseek_models) - the endpoint entry stays in registry.endpoints and
    owns the /models probe; the children own queue groups.
    """

    def __init__(self, instance: LlamaServerInstance, allow_spawn: bool = True):
        self.instance = instance
        self.model_id: Optional[str] = getattr(instance, "model_id", None)
        self.key = (instance.host.lower(), instance.port, self.model_id) if self.model_id \
            else (instance.host.lower(), instance.port)
        self.api_url: str = instance.api_url
        self.local: bool = _is_local(instance.host)
        # (2026-11 strata / deepseek) Backend kind: "llama" for llama-server, "strata" for a
        # Strata endpoint, "deepseek" for the hosted DeepSeek API. Both authenticated kinds
        # are attach-only, so the brain only probes them and signs every request.
        self.kind: str = getattr(instance, "kind", "llama")
        self.is_deepseek: bool = self.kind == "deepseek"
        self.api_key: Optional[str] = getattr(instance, "api_key", None)
        self.headers: Optional[dict] = (deepseek_auth_headers(self.api_key)
                                        if self.is_deepseek else strata_auth_headers(self.api_key))
        # LOCAL llama nodes may be spawned by us; everything else (remotes, Strata, DeepSeek)
        # is ATTACH-ONLY: a failed probe must never start or restart a process we do not own.
        # allow_spawn is False for the "dynamic" fleet: it never spawns, it only attaches to
        # whatever is already available (a down local llama node stays down and grayed out).
        self.spawnable: bool = allow_spawn and self.local and self.kind == "llama"
        # (2026-10-25) True only when WE launched this instance's llama-server at boot
        # (tracked Popen in process_mgmt). Attached foreign nodes keep False so stop_all()
        # and the discovery relaunch never kill a node an admin started.
        self.spawned_by_us: bool = False
        self.healthy: Optional[bool] = None      # None = not probed yet
        self.busy: int = 0                       # requests currently running on this backend
        self._provider = None                    # lazy LlamaProvider handle

        # --- discovered via /props (2026-10 fleet) --------------------------------
        self.total_slots: Optional[int] = None   # what llama.cpp reports (--parallel N)
        # n_ctx of ONE slot. A configured value on the instance (llama_node.json /
        # strata_servers.json 'ctx_per_slot') is the starting point; /props overwrites it
        # with what the node actually reports.
        self.ctx_per_slot: Optional[int] = getattr(instance, "ctx_per_slot", None)
        self.model_key: str = ""                 # canonical routing key (see model_identity)
        # (2026-11 strata) Extra canonical keys this backend must route: an endpoint can
        # name the same model under an alias id (/props.model_alias, /v1/models ids,
        # llama's router aliases). Clients pick those ids from the catalog.
        self.model_aliases: set[str] = set()
        self.model_label: str = ""               # display basename
        self.build: str = ""                     # llama.cpp build_info
        # What this node's baked chat template lets a request control about reasoning.
        # Discovered from /props (template text + caps), never hardcoded: one model offers
        # enable_thinking + reasoning_effort in its own words, another only a toggle,
        # another nothing at all. An entry's optional 'thinking' block pins it.
        self.thinking: ThinkingPolicy = ThinkingPolicy.none()
        self.thinking_override = getattr(instance, "thinking", None)
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
        self._warned_thinking = False

        # --- DeepSeek endpoint state -------------------------------------------
        # On an ENDPOINT backend (model_id None): the parsed /models entries. A child
        # backend (model_id set) mirrors its endpoint's health and carries that model's
        # context window + thinking policy.
        self.endpoint_models: list = []
        self.parent: Optional["LlmBackend"] = None

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
        """Concurrent-request capacity for routing: discovered /props.total_slots when no config
        value is set; otherwise min(config value, reported) so we never over-admit. Never exceeds
        LLAMA_MAX_CONCURRENCY.
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
        StrataProvider, DeepSeek endpoints get DeepSeekProvider (its own body transform).
        """
        if self._provider is None:
            from config import PROVIDER_DEFAULT_TIMEOUT
            if self.kind == "strata":
                from ..strata.provider import StrataProvider
                self._provider = StrataProvider(self.api_url, PROVIDER_DEFAULT_TIMEOUT,
                                                headers=self.headers)
            elif self.kind == "deepseek":
                from ..deepseek.provider import DeepSeekProvider
                self._provider = DeepSeekProvider(self.api_url, PROVIDER_DEFAULT_TIMEOUT,
                                                  headers=self.headers)
            else:
                from .provider import LlamaProvider
                self._provider = LlamaProvider(self.api_url, PROVIDER_DEFAULT_TIMEOUT)
        return self._provider

    def serves_model(self, model_key: str) -> bool:
        """True when requests for this canonical model key may run on this backend.

        One endpoint serves one model under several ids (/props file name, /props.model_alias and
        /v1/models alias), and all of them must route to the same slot group or the worker's
        routing guard rejects every request.

        A DeepSeek ENDPOINT backend serves every model its /models lists, so it answers True
        for any of them; its per-model CHILD backend is the exact match. Routing uses the
        children, so an endpoint-level match never runs a request itself.
        """
        if not model_key:
            return False
        key = model_key_of(model_key)
        if self.model_id is not None:
            return key == self.model_key or key in self.model_aliases
        if self.is_deepseek:
            return any(m["key"] == key for m in self.endpoint_models)
        return key == self.model_key or key in self.model_aliases

    # --- discovery (/props) -------------------------------------------------------

    async def refresh_props(self) -> bool:
        """Read /props: slots, per-slot context, loaded model and llama.cpp build.

        This is how the brain learns what a node runs. Failures are recorded in props_error but
        never mark a backend unhealthy - /health owns that decision.
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
            # Thinking controls read from the template text itself (the only trustworthy
            # source) and folded with chat_template_caps; an entry's 'thinking' block pins
            # anything it states.
            #
            # A policy demoted at runtime (streaming._demote_thinking: the endpoint rejected
            # our thinking keys) survives this re-discovery - otherwise the props poll would
            # resurrect a knob proved broken and every turn would fail again. An operator
            # 'thinking' block overrides that: pinning it states the controls really work.
            demoted = self.thinking if self.thinking.source == "runtime" else None
            policy = ThinkingPolicy.from_props(data, model_name=self.model_label or raw_model)
            resolved = ThinkingPolicy.from_override(self.thinking_override, policy)
            if demoted is not None and self.thinking_override is None:
                self.thinking = demoted
            else:
                self.thinking = resolved
                if resolved.source == "template" and not self._warned_thinking:
                    self._warned_thinking = True
                    logger.info(
                        "[REGISTRY] %s thinking controls: %s%s (model values %s)",
                        self.label, resolved.levels(),
                        f" preserve={resolved.preserve}" if resolved.preserve else "",
                        resolved.efforts or "none",
                    )
        except Exception as e:  # malformed /props payload - keep whatever we already had
            self.props_error = f"unparseable /props: {e}"
            return False

        # A node that exposes no template knob can still SEPARATE reasoning out when it
        # was started with --reasoning (LLAMA_SERVER_REASONING): nothing can then turn
        # thinking off per request, but the UI may still offer to display it.
        if self.thinking.source == "none" and _startup_reasoning_enabled():
            self.thinking = ThinkingPolicy(
                emit_only=True, source="emit",
                hint="reasoning is separated by the node's --reasoning flag; "
                     "the request cannot change it")

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
        """One health round-trip; marks healthy/unhealthy.

        llama/Strata: /health (+ /props on success). DeepSeek endpoint: GET /models is the
        health probe AND the discovery step - it lists the model ids this account serves, each
        becoming a routable child backend (registry.sync_deepseek_models). A child backend just
        mirrors its endpoint's health.

        Discovery is refreshed before the healthy flag/event is published, so workers waking on
        the event already see the current model key and capacity.
        """
        if self.parent is not None:
            self.healthy = bool(self.parent.healthy)
            if self.healthy:
                self._healthy_event.set()
            else:
                self._healthy_event.clear()
            return bool(self.healthy)

        if self.is_deepseek:
            return await self._probe_deepseek()

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
                " (remote — attach-only)" if self.kind == "llama" else f" ({self.kind.title()} — attach-only)")
            logger.info("[REGISTRY] Backend %s is now %s%s", self.label, state, note)
            self._notify_health_changed()
        return bool(self.healthy)

    async def _probe_deepseek(self) -> bool:
        """GET /models: health + model discovery for a DeepSeek endpoint.

        DeepSeek has no /health or /props, so the model list IS the health answer: a 200
        with at least one model means the account is reachable and routable. Each model
        becomes a child backend (one queue group + workers per id), so switching models is
        picking another id - nothing is loaded or reloaded.
        """
        from ..deepseek.models import fetch_models

        try:
            models = await fetch_models(self.api_url, self.headers)
        except Exception as e:
            logger.debug("[REGISTRY] %s /models probe failed: %s", self.label, e)
            self.props_error = str(e)
            models = []

        if not models:
            self.props_error = self.props_error or "/models returned no models"
            if not getattr(self, "_warned_props", False):
                self._warned_props = True
                logger.warning(
                    "[REGISTRY] %s answered no models (%s)%s - not routing to it until "
                    "GET /models works (set 'api_key' in config/deepseek_servers.json or "
                    "config.DEEPSEEK_API_KEY)",
                    self.label, self.props_error,
                    "" if self.headers else " - this endpoint has NO Bearer key",
                )
            if self.parent is None:
                self._set_endpoint_health(False)
            else:
                self.healthy = False
                self._healthy_event.clear()
            return False

        self.endpoint_models = models
        self.props_at = time.time()
        self.props_error = ""
        if self.parent is None:
            self._set_endpoint_health(True)
        else:
            self.healthy = True
            self._healthy_event.set()
        return True

    def _set_endpoint_health(self, ok: bool) -> None:
        """Publish an endpoint's health onto every child model backend it owns."""
        reg = get_registry()
        children = reg.children_of(self) if reg is not None else []
        previous = bool(self.healthy)
        self.healthy = bool(ok)
        if self.healthy:
            self._healthy_event.set()
        else:
            self._healthy_event.clear()
        for c in children:
            c.healthy = bool(ok)
            if ok:
                c._healthy_event.set()
            else:
                c._healthy_event.clear()
        if self.healthy != previous:
            state = "HEALTHY" if self.healthy else "UNHEALTHY"
            note = "" if self.spawnable else (
                " (remote — attach-only)" if self.kind == "llama"
                else f" ({self.kind.title()} — attach-only)")
            logger.info("[REGISTRY] Backend %s is now %s%s", self.label, state, note)
            self._notify_health_changed()

    def _notify_health_changed(self) -> None:
        """Tell the registry a health transition happened (drives the fleet_status push)."""
        reg = get_registry()
        if reg is not None:
            reg.fire_health_change()

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
        """Record a failure against this node (for the table and error frames)."""
        self.last_error = str(message or "")[:500]
        self.error_count += 1

    def mark_unhealthy(self, reason: str = "") -> None:
        """Immediately take this node out of routing (a failed request beats a 30 s poll).

        The health poller re-probes and will bring it back automatically when it recovers.
        A DeepSeek model child shares its endpoint's fate: a failure there (bad key,
        unreachable host) takes the whole endpoint down, so every model group goes with it.
        """
        if self.parent is not None:
            self.parent.mark_unhealthy(reason)
            return
        if reason:
            self.bump_error(reason)
        was_healthy = bool(self.healthy)
        if was_healthy:
            logger.warning("[REGISTRY] %s marked UNHEALTHY now: %s", self.label, reason or "request failed")
        self.healthy = False
        self._healthy_event.clear()
        if self.is_deepseek:
            self._set_endpoint_health(False)
        elif was_healthy:
            self._notify_health_changed()

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
            "thinking": self.thinking.describe(),
        }

    # --- lifecycle ------------------------------------------------------------

    async def ensure_started(self) -> bool:
        """Make sure this instance is up: local = reconcile (attach or spawn), remote = probe.

        The spawn --parallel count is the explicit config value, else LLAMA_SERVER_PARALLEL;
        after /props lands the discovered total_slots owns routing capacity.
        """
        if not self.spawnable:
            return await self.probe_health()

        # (2026-10-25) Node-launcher reconcile when a VALID node config describes THIS port.
        # When it does, its verdict is FINAL: auto_start=false must never be overridden by a
        # legacy spawn, and a failed reconcile (foreign process on the port, model missing...)
        # must not be papered over by spawning with generic defaults - the health poller
        # keeps probing so a manually-fixed node comes back without a SERVER restart.
        from . import node_launcher
        cfg = None
        try:
            if node_launcher.load_raw_config() is not None:
                cfg = node_launcher.load_config()
        except RuntimeError as e:
            logger.warning("[REGISTRY] %s unreadable/invalid (%s) - using the legacy spawn path",
                           self.label, e)

        use_node_cfg = (cfg is not None and int(cfg["port"]) == self.instance.port)
        if use_node_cfg:
            try:
                spawned, ok = await node_launcher.ensure_local_node(self.instance)
            except Exception as e:  # pragma: no cover - defensive (ensure never raises)
                logger.error("[REGISTRY] %s node-launcher reconcile failed: %s", self.label, e)
                spawned, ok = False, False
            self.spawned_by_us = bool(spawned and ok)
            if not ok:
                self.healthy = False
                return False
            # Same ordering rule as probe_health: discovery BEFORE workers can wake.
            props_ok = await self.refresh_props()
            self.healthy = bool(props_ok)
            if self.healthy:
                self._healthy_event.set()
            else:
                self._healthy_event.clear()
            return bool(self.healthy)

        from .server import start_server
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

        # Legacy path spawned it ourselves (start_server tracks the Popen).
        self.spawned_by_us = True

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
                from .process_mgmt import (_server_processes, _get_server_process,
                                           _terminate_process, wait_for_port_free_async)
                async with self.restart_lock:
                    if not (self.spawned_by_us or _get_server_process(self.key) is not None):
                        # (2026-10-25) ATTACHED node we did not spawn - NEVER kill it.
                        logger.warning(
                            "[REGISTRY] %s reports %d slot(s) but was launched with --parallel %d "
                            "- it is an ATTACHED node we did not spawn, so it is NOT killed; "
                            "routing capacity follows /props",
                            self.label, reported, launch_slots,
                        )
                    else:
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
    """All configured backends, started together and health-polled.

    from_config(kind="strata") builds the same registry over config/strata_servers.json:
    every backend then reports kind="strata" and is attach-only.
    from_config(kind="deepseek") builds it over config/deepseek_servers.json: each entry is a
    hosted ENDPOINT. Because one endpoint serves several model ids, the registry keeps the
    endpoint in ``endpoints`` (it owns the /models probe) and expands it into one routable
    CHILD backend per model id in ``backends`` (one queue group + workers per id), so the
    picker can switch between models and routing stays exact.
    from_config(kind="dynamic") merges all three config files into one mixed fleet.
    """

    def __init__(self, instances: list[LlamaServerInstance], spawn_enabled: bool = True):
        # Source of truth the registry was built from ("llama" | "strata" | "deepseek" |
        # "dynamic"). Set by from_config(); "mixed" for direct construction. build_model_catalog
        # reads it to decide whether to merge the last-known offline models.
        self.kind: str = "mixed"
        # (2026-11 strata outage) Health-change listeners: async callbacks(snapshot)
        # fired when a backend flips healthy/unhealthy and on each poller heartbeat.
        # The WS layer subscribes here to push live 'fleet_status' frames to clients.
        self._health_change_cbs: list = []
        # de-dup by key (loader already dedupes; defensive for direct construction)
        seen = set()
        self.backends: dict[tuple, LlmBackend] = {}
        # DeepSeek ENDPOINT backends (model_id None): probes /models and own their children.
        self.endpoints: dict[tuple, LlmBackend] = {}
        for inst in instances:
            b = LlmBackend(inst, allow_spawn=spawn_enabled)
            if b.key not in seen:
                seen.add(b.key)
                if b.is_deepseek and b.model_id is None:
                    self.endpoints[b.key] = b
                else:
                    self.backends[b.key] = b

    @classmethod
    def from_config(cls, kind: str = "llama") -> "LlmBackendRegistry":
        """Build the registry from a fresh read of the backend's config file.

        ``kind`` selects the source of truth: "llama" -> config/llama_servers.json
        (spawn-or-attach llama-server instances), "strata" -> config/strata_servers.json
        (attach-only endpoints), "deepseek" -> config/deepseek_servers.json (attach-only
        hosted endpoints, expanded per served model id), "dynamic" -> all three files merged
        into one mixed fleet (llama + strata + deepseek; the picker lists every model any
        endpoint serves). The dynamic fleet is ATTACH-ONLY even for local llama nodes: it
        never spawns a service, it only uses what is already available (a down node stays
        down and its models gray out in the picker).
        """
        if kind == "strata":
            instances = get_strata_server_instances()
        elif kind == "deepseek":
            instances = get_deepseek_server_instances()
        elif kind == "dynamic":
            instances = (
                list(get_llama_server_instances())
                + list(get_strata_server_instances())
                + list(get_deepseek_server_instances())
            )
        else:
            instances = get_llama_server_instances()
        reg = cls(instances, spawn_enabled=(kind != "dynamic"))
        reg.kind = kind
        return reg

    # --- DeepSeek model expansion -------------------------------------------

    def sync_deepseek_models(self) -> int:
        """Create/update one routable child backend per model id of every DeepSeek endpoint.

        Idempotent: an existing child for a model id is updated in place (its ctx and
        thinking follow the latest /models entry, its health follows the endpoint); a model
        that disappeared from /models loses its child, so its queue group stops being
        advertised. Returns the number of child backends now routable.
        """
        from ..deepseek.thinking import DeepSeekThinkingPolicy

        for endpoint in self.endpoints.values():
            for m in endpoint.endpoint_models:
                key = (endpoint.instance.host.lower(), endpoint.instance.port, m["key"])
                child = self.backends.get(key)
                if child is None:
                    inst = endpoint.instance
                    child = LlmBackend(_deepseek_child_instance(inst, m["key"], m["ctx"]))
                    child.parent = endpoint
                    self.backends[key] = child
                    logger.info("[REGISTRY] DeepSeek model '%s' is routable (%s)",
                                m["id"], endpoint.label)
                else:
                    child.ctx_per_slot = m["ctx"] or child.ctx_per_slot
                child.model_key = m["key"]
                child.model_label = m["id"]
                child.model_aliases = set()
                child.thinking = DeepSeekThinkingPolicy.from_model(
                    m.get("raw") or {}, endpoint.thinking_override)
                child.healthy = bool(endpoint.healthy)
                if child.healthy:
                    child._healthy_event.set()
                else:
                    child._healthy_event.clear()

            gone = [k for k, b in self.backends.items()
                    if b.parent is endpoint and not any(
                        m["key"] == b.model_key for m in endpoint.endpoint_models)]
            for k in gone:
                b = self.backends.pop(k)
                logger.info("[REGISTRY] DeepSeek model '%s' is no longer served - its queue "
                            "group is removed (%s)", b.model_label, endpoint.label)

        return len(self.backends)

    # --- accessors ------------------------------------------------------------

    def primary(self) -> LlmBackend:
        """First configured backend - the entry single-instance code paths use.
        """
        if self.backends:
            return next(iter(self.backends.values()))
        return next(iter(self.endpoints.values()))

    def get_by_key(self, key: tuple) -> Optional[LlmBackend]:
        return self.backends.get(key) or self.endpoints.get(key)

    def get_for_api_url(self, api_url: str, model: str = "") -> Optional[LlmBackend]:
        """The backend a request for *api_url* (and optionally *model*) will run on.

        llama/Strata: the (host, port) entry. DeepSeek: the per-model CHILD backend when a
        model is named (exact ctx + thinking for that id), else the endpoint itself.
        """
        k = key_for_api_url(api_url)
        if not k:
            # A hosted URL with no explicit port ("https://api.deepseek.com") has no
            # (host, port) key - match the backend whose api_url IS this endpoint instead,
            # so DeepSeek routing, health and thinking stay exact.
            wanted = (api_url or "").strip().rstrip("/").lower()
            k = next((b.key[:2] for b in list(self.endpoints.values()) + list(self.backends.values())
                      if b.api_url.rstrip("/").lower() == wanted), None)
            if not k:
                return None
        endpoint = self.endpoints.get(k)
        if endpoint is not None and model:
            want = model_key_of(model)
            child = self.backends.get((k[0], k[1], want))
            if child is not None:
                return child
        backend = self.backends.get(k) or endpoint
        if backend is None and model:
            want = model_key_of(model)
            for b in self.backends.values():
                if b.key[:2] == k and b.serves_model(want):
                    return b
        return backend

    def children_of(self, endpoint: LlmBackend) -> list[LlmBackend]:
        """The per-model child backends of one DeepSeek endpoint."""
        return [b for b in self.backends.values() if b.parent is endpoint]

    def healthy_backends(self) -> list[LlmBackend]:
        return [b for b in self.backends.values() if b.healthy]

    def backends_for_model(self, model: str) -> list[LlmBackend]:
        """Backends whose discovered model matches *model* (healthy or not).

        Matches the canonical key AND the endpoint's alias ids, so a
        client that picked the alias from ``/v1/models`` lands in this backend's queue.
        """
        want = model_key_of(model)
        if not want:
            return []
        return [b for b in self.backends.values() if b.serves_model(want)]

    def model_configured(self, model: str) -> bool:
        """True when at least one node is CONFIGURED for this model (healthy or not)."""
        return bool(self.backends_for_model(model))

    def canonical_model_key(self, model: str) -> str:
        """The queue key a request for *model* must use.

        Workers poll one queue per backend's canonical model_key, so an alias id from the client
        is re-keyed to the group that serves it. Falls back to the caller's key when no backend
        claims the model, which is then reported as "no node serves this".
        """
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

    def _row(self, b: LlmBackend) -> dict:
        """One fleet-table row for a backend or a DeepSeek endpoint."""
        return {
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
            # what a request to this node may control about reasoning
            "thinking": b.thinking.describe(),
        }

    def snapshot(self) -> list[dict]:
        """Per-backend state for the table / logs / error frames.

        Routable backends (one per model for DeepSeek) plus any DeepSeek endpoint with no
        children yet - that row is the endpoint itself, so a down endpoint still shows its
        error instead of an empty table.
        """
        rows = [self._row(b) for b in self.backends.values()]
        for e in self.endpoints.values():
            if not self.children_of(e):
                rows.append(self._row(e))
        return rows

    def thinking_for_model(self, model: str) -> ThinkingPolicy:
        """Thinking policy of every node serving *model*, merged (ThinkingPolicy.merge).

        The intersection: the catalog must offer only levels every serving node accepts, or a
        request routed to the weaker node fails at template render time. Healthy state is
        irrelevant - a node that is merely down still defines what the template allows.

        DeepSeek policies merge through DeepSeekThinkingPolicy.merge so the merged policy keeps
        the canonical->effort mapping (ThinkingPolicy.merge builds a plain policy, which would
        lose it and send an effort DeepSeek does not accept).
        """
        policies = [b.thinking for b in self.backends_for_model(model)]
        if policies and all(type(p).__name__ == "DeepSeekThinkingPolicy" for p in policies):
            from ..deepseek.thinking import DeepSeekThinkingPolicy
            return DeepSeekThinkingPolicy.merge(policies)
        return ThinkingPolicy.merge(policies)

    # --- lifecycle ------------------------------------------------------------

    async def _probe_one(self, backend: LlmBackend) -> bool:
        try:
            return await backend.probe_health()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("[REGISTRY] probe error for %s: %s", backend.label, e)
            return False

    async def probe_all(self) -> list[tuple[LlmBackend, bool]]:
        """Probe every routable backend + every DeepSeek endpoint, IN PARALLEL.

        A of down nodes would otherwise cost 5 s each (connect timeout) before the
        server can accept its first client. DeepSeek endpoints are probed (GET /models)
        and their per-model children are (re)built from the answer afterwards.
        """
        targets = [e for e in self.endpoints.values()] + [
            b for b in self.backends.values() if b.parent is None
        ]
        results = await asyncio.gather(*(self._probe_one(b) for b in targets))
        if self.endpoints:
            self.sync_deepseek_models()
        self._remember_models()
        # Heartbeat: push a fresh fleet snapshot even when nothing flipped (keeps an open
        # outage dialog's slot counts / busy state current between transitions).
        self.fire_health_change()
        return list(zip(targets, results))

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

    def _kinds(self) -> set[str]:
        """Every backend kind present, endpoints included (a down DeepSeek endpoint
        still has no children yet, and its wording must not fall back to 'llama')."""
        kinds = {b.kind for b in self.backends.values()}
        kinds |= {e.kind for e in self.endpoints.values()}
        return kinds

    def registry_title(self) -> str:
        """Operator-facing registry title naming the config file the backends came from."""
        kinds = self._kinds()
        if kinds == {"strata"}:
            return "STRATA BACKEND REGISTRY (from config/strata_servers.json)"
        if kinds == {"deepseek"}:
            return "DEEPSEEK BACKEND REGISTRY (from config/deepseek_servers.json)"
        if kinds == {"llama"}:
            return "LLAMA BACKEND REGISTRY (from config/llama_servers.json)"
        return "LLM BACKEND REGISTRY (mixed: " + ", ".join(sorted(kinds)) + ")"

    def backend_noun(self) -> str:
        """Client-facing wording for a serving backend, chosen by kind.

        The wording differs per kind because what the user must do differs: start a llama
        node, start Strata, or fix the key of a hosted DeepSeek endpoint.
        """
        kinds = self._kinds()
        if kinds == {"strata"}:
            return "Strata endpoint"
        if kinds == {"deepseek"}:
            return "DeepSeek endpoint"
        if kinds == {"llama"}:
            return "llama node"
        return "backend"

    def _remember_models(self) -> None:
        """Persist last-known served models (see backend_model_cache.remember).

        Best-effort and lazy-imported: a cache write failure or an import problem must never
        disturb health probing or routing.
        """
        try:
            from .backend_model_cache import remember
            remember(self)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("[REGISTRY] last-known model cache update skipped: %s", e)

    async def start_all(self) -> bool:
        """Start/attach every configured instance and log the startup table.

        DeepSeek endpoints are attached first (GET /models) and expanded into their
        per-model child backends, so the fleet table lists one row per served model id.

        Returns True when at least one backend is healthy (the SERVER can serve).
        """
        results = []
        for e in self.endpoints.values():
            ok = await e.ensure_started()
            results.append((e, ok))
        if self.endpoints:
            self.sync_deepseek_models()

        for b in self.backends.values():
            if b.parent is not None:
                continue   # its endpoint already probed it
            ok = await b.ensure_started()
            results.append((b, ok))

        self.log_fleet_table(self.registry_title())

        # Persist what we discovered so a down endpoint's models still gray out after a
        # restart (they are only learned when the endpoint answers, which a down one never does).
        self._remember_models()

        any_ok = any(ok for _, ok in results)
        if not any_ok:
            logger.error("[REGISTRY] No backend is available - chat requests will fail "
                         "until one becomes healthy")
        return any_ok

    async def stop_all(self) -> None:
        """Stop the instances WE spawned; remotes and attached nodes keep running.

        Only backends with spawned_by_us=True and a live tracked Popen are terminated.
        Attach-only backends (spawnable=False: remotes and every Strata endpoint) are skipped.
        """
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

        Safe to call again on a DIFFERENT event loop: the SERVER boots on one temporary
        asyncio.run loop and then serves on another, so a poller bound to the boot loop
        would die there and freeze health + capacity forever.
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
                await self.probe_all()

        self._poller_task = asyncio.create_task(_poll_loop(), name="llama-registry-health-poller")
        logger.info("[REGISTRY] Health poller started (every %ds)", interval_sec)
        return self._poller_task

    def stop_health_poller(self) -> None:
        task = getattr(self, "_poller_task", None)
        if task is not None and not task.done():
            task.cancel()

    # --- health-change listeners (2026-11 strata outage) --------------------------

    def add_health_change_listener(self, cb) -> None:
        """Register an async callback(snapshot) fired on every health transition and on each
        health-poller heartbeat. The callback receives the fleet snapshot (list of dicts)."""
        if cb not in self._health_change_cbs:
            self._health_change_cbs.append(cb)

    def remove_health_change_listener(self, cb) -> None:
        try:
            self._health_change_cbs.remove(cb)
        except ValueError:
            pass

    def fire_health_change(self, snapshot: Optional[list] = None) -> None:
        """Schedule every listener with the current snapshot. Best-effort: a listener error
        or a missing running loop must never disturb health polling."""
        if snapshot is None:
            snapshot = self.snapshot()
        for cb in list(self._health_change_cbs):
            coro = None
            try:
                coro = cb(snapshot)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("[REGISTRY] health-change listener error: %s", e)
                continue
            if coro is None or not hasattr(coro, "__await__"):
                continue
            try:
                asyncio.get_running_loop().create_task(coro)
            except RuntimeError:  # pragma: no cover - no running loop (sync caller)
                try:
                    asyncio.ensure_future(coro)
                except RuntimeError:
                    pass

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
