"""RemoteToolOrchestrator - drop-in replacement for ToolOrchestrator.

Tool source is fetched from the SERVER on demand over WebSocket; the CLIENT starts with
lazy fetch on the first get_ollama_tools(), source per tool, compiled with working_root
injected. Shared modules become a 'tools' namespace package in sys.modules so sibling
imports resolve, and the SERVER's shared_deps / config_constants reach compile_tool().
"""

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

from .tool_cache import ToolCache
from .tool_fetcher import ToolFetcher
from .dynamic_loader import DynamicModuleLoader, ToolCompilationError

logger = logging.getLogger("COOLEMS.Tools.Remote.Orchestrator")


def _def_name(d: Any) -> str:
    """Tool name from a definition dict ('' when malformed)."""
    return d.get("function", {}).get("name", "") if isinstance(d, dict) else ""


#: The escalation escape hatch. Always present in an intent-gated starter set so the
#: model can pull any catalogued tool's full schema on demand (see inject_tools + the
#: react_loop request_tools intercept). A control-flow tool owned by the loop — never
#: dispatched to ToolExecutor.
REQUEST_TOOLS_DEFINITION = {
    "type": "function",
    "function": {
        "name": "request_tools",
        "description": (
            "Add more tools to this turn's available schema. Call this when you need a "
            "tool that is listed in the catalog but not available yet. Pass the exact "
            "tool names; each tool's whole category is loaded so related tools come together."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact tool names to make available (from the catalog).",
                },
            },
            "required": ["names"],
        },
    },
}

#: User-intent -> tool folders for the per-turn starter set. Intent names come from
#: app.detection.detect_tool_requirement(); folders are the SERVER's on-disk tool folders
#: (tools_response.tool_categories). Tools outside these folders are reached on demand
#: via request_tools.
_INTENT_CATEGORY_MAP = {
    "web": {"web_tools", "search_tools"},
    "browser": {"web_interact"},
    "file": {"file_tools", "edit_tools", "search_tools", "exec_tools", "test_tools", "git_tools"},
    "pdf": {"file_tools", "edit_tools", "exec_tools"},
    "ocr": {"vision_tools"},
    "calc": {"exec_tools"},
    "time": {"time_tools"},
}

#: CLIENT-local tools are never in the SERVER catalog (the SERVER does not know them).
#: Appended to the catalog so the model can still discover + request them. Dynamic
#: mcp__<server>__<tool> names are described by the per-turn MCP notes instead.
_LOCAL_TOOL_HINTS = (
    "\n\n## CLIENT-local (also available on request)\n"
    "- ask_user_question — ask the user a question on a card (blocking or timed)\n"
    "- list_mcp_servers / list_mcp_resources / list_mcp_resource_templates / "
    "read_mcp_resource — inspect connected MCP servers and read their resources"
)


class RemoteToolOrchestrator:
    """Remote tool orchestrator - fetches tools from SERVER on-demand.

    LAZY: tools are fetched only when first needed, which avoids event-loop conflicts at
    startup and keeps tools available."""

    def __init__(self, provider=None, ollama_url: str = None):
        self.provider = provider
        self.ollama_url = ollama_url

        # Core components
        self._cache = ToolCache()
        self._fetcher = ToolFetcher(provider) if provider else None
        self._loader = DynamicModuleLoader()  # working root is published per turn via the tools.utils ContextVar (2026-10-09)

        # Runtime tool registry (name -> callable)
        self.tools: Dict[str, Callable] = {}

        # Config constants from SERVER (shared across ALL tools)
        self._config_constants: Dict[str, Any] = {}

        # Prompts from SERVER — stored in memory only, never saved to disk
        self._system_prompt: str = ""
        self._chat_prompt: str = ""

        # Initialization tracking (2026-08-29 threadless refactor): no
        # threading.Lock and no executor. Everything runs on the single event
        # loop, so overlapping callers are deduplicated by sharing one in-flight
        # asyncio task instead of locking a worker thread.
        self._initialized = False
        self._init_task: Optional[asyncio.Task] = None

        logger.info("RemoteToolOrchestrator initialized (zero tools — will fetch from SERVER on demand)")

    # NOTE: no set_working_root() here on purpose - each workspace's folder lives in its own
    # conversations.working_root DB row; agentic_mode() resolves the active turn's value and
    # publishes it via set_current_working_root(), so nothing has to be propagated (2026-10-09).

    # --- Lazy initialization (called automatically when first needed) ---

    async def _ensure_initialized(self) -> bool:
        """Fetch tools from the SERVER if not already done - single loop, no threads.

        One shared in-flight task dedupes overlapping callers; nothing needs a lock because
        only the event-loop thread touches this state."""
        # Fast path - already initialized with data
        if self._initialized and self._cache.get_definitions():
            return True

        # Another coroutine is already fetching — share its result (dedupe).
        if self._init_task is not None and not self._init_task.done():
            try:
                await self._init_task
            except asyncio.CancelledError:
                logger.error("[INIT] Lazy init task cancelled (timeout) — will retry on next call")
                return False
            except Exception as e:
                logger.error(f"[INIT] Lazy init failed while sharing in-flight task: {e}")
                return False
            return bool(self._cache.get_definitions())

        # Start the fetch on THIS loop. create_task accepts a task 'name' since
        # Python 3.8; fall back to ensure_future (no name) for older venvs.
        try:
            self._init_task = asyncio.create_task(
                self.initialize_from_server(), name="remote-tools-init"
            )
        except TypeError:  # pre-3.8 loop without the name kwarg
            self._init_task = asyncio.ensure_future(self.initialize_from_server())
        try:
            count = await asyncio.wait_for(asyncio.shield(self._init_task), timeout=30)
        except asyncio.TimeoutError:
            logger.error("[INIT] Lazy init TIMEOUT — SERVER did not respond within 30s")
            self._init_task.cancel()
            return False
        except Exception as e:
            logger.error(f"[INIT] Lazy init ERROR — {e}", exc_info=True)
            return False
        finally:
            # The task object is done; drop the reference so a later call
            # (after a failure, or after clear_tools()) starts a fresh fetch.
            self._init_task = None

        if count > 0:
            self._initialized = True
            logger.info(f"[INIT] Lazy init SUCCESS — {count} tools loaded from SERVER")
            return True
        else:
            logger.error("[INIT] Lazy init FAILED — SERVER returned 0 tools")
            return False

    async def initialize_from_server(self) -> int:
        """Fetch allowed tool definitions + shared modules from the SERVER.

        Returns the number of tools. Awaited on the running loop; install_from_response() does
        the bookkeeping (definitions, shared modules, DNA data, config constants, prompt)."""
        if not self._fetcher or not self.provider:
            logger.error("Cannot initialize — no provider connected")
            return 0

        try:
            result = await self._fetcher.fetch_tools()
        except Exception as e:
            logger.error(f"Failed to fetch tool definitions from SERVER: {e}", exc_info=True)
            return 0

        if result is None:
            logger.error("fetch_tools returned None — SERVER may be unavailable")
            return 0

        try:
            return self.install_from_response(result)
        except Exception as e:
            logger.error(f"Failed to process tools_response from SERVER: {e}", exc_info=True)
            return 0

    def install_from_response(self, result: Dict[str, Any]) -> int:
        """Install a SERVER tools_response payload (NO network I/O).

        Separate so the boot bootstrap can reuse it: app/providers/bootstrap.py fetches this
        response before CoolemsClientProvider exists. Returns the number of tools;
        install_shared_modules() is idempotent."""
        try:
            definitions = result.get("definitions", [])
            allowed_tools = result.get("allowed_tools")

            # Handle shared modules from SERVER
            # NEW: 'shared_sources' dict with all module sources (utils, path_guard, etc.)
            shared_sources = result.get("shared_sources", {})

            # LEGACY fallback: if server only sent single-string shared_utils_source
            legacy_shared = result.get("shared_utils_source")
            if not shared_sources and legacy_shared:
                shared_sources = {'utils': legacy_shared}

            # FIX (2026-01-31): Store config constants from SERVER for use during compilation
            self._config_constants = result.get("config_constants", {})
            if self._config_constants:
                logger.info(f"[INIT] Received {len(self._config_constants)} config constants from SERVER")

            # FIX (2026-08-20): per-profile python_exec blocked-libs set shipped by the
            # SERVER in tools_response. Stored into _config_constants so compile_tool()
            # injects it as a global named PYTHON_EXEC_BLOCKED_LIBS -- python_exec reads it
            # first and enforces exactly this profile's import constraints on the CLIENT.
            # isinstance check is MANDATORY: an EMPTY list (admin clean set) is meaningful
            # data ("allow everything"), not a missing value.
            if "python_exec_blocked_libs" in result and result["python_exec_blocked_libs"] is not None:
                self._config_constants["PYTHON_EXEC_BLOCKED_LIBS"] = result["python_exec_blocked_libs"]
                _bl_count = len(self._config_constants["PYTHON_EXEC_BLOCKED_LIBS"])
                logger.info(f"[INIT] python_exec blocked-libs set from SERVER: {_bl_count} module(s)"
                            + (" (EMPTY = allow everything)" if _bl_count == 0 else ""))

            if not definitions:
                logger.warning("SERVER sent empty tool list (no tools available)")
                return 0

            # Store in cache
            self._cache.set_definitions(definitions, allowed_tools)

            # Tool taxonomy + catalog: the SERVER ships the per-tool folder and a compact
            # grouped text index. The folder map drives starter_tools(); the catalog is
            # appended to the prompt so the model can discover tools without paying for
            # their full JSON schema.
            self._cache.set_categories(result.get("tool_categories", {}))
            self._cache.set_catalog(result.get("tool_catalog", ""))

            # FIX (2026-08-17): DNA data files from SERVER -- synced into the CLIENT's own
            # app/dna directory (NEVER working_root) so delivered app.dna.* modules read them there.
            dna_data = result.get("dna_data", {})
            if dna_data:
                self._loader.set_pending_dna_data(dna_data)

            # FIX (2026-07-18): give the loader the SERVER config constants BEFORE installing
            # shared modules so delivered code with module-level 'from config import X' lines
            # (e.g. a delivered tool's MODEL_NAME) can be rewritten to real assignments.
            self._loader.set_config_constants(self._config_constants)

            # (2026-08-20 fail-closed integrity): SERVER ships SHA-256 prefixes for every
            # shared/framework module. The loader refuses to exec any delivered source whose
            # hash does not match -- unverified code is never run when a hash was promised.
            shared_hashes = result.get("shared_hashes", {})
            if shared_hashes:
                self._loader.set_expected_shared_hashes(shared_hashes)
                logger.info(f"[INIT] Received {len(shared_hashes)} integrity hashes from SERVER (fail-closed)")

            # Install ALL shared modules into the loader (not just utils)
            if shared_sources:
                success = self._loader.install_shared_modules(shared_sources, expected_hashes=shared_hashes or None)
                if not success:
                    logger.warning("Failed to install some shared modules from SERVER")
                else:
                    module_names = [k for k, v in shared_sources.items() if v]
                    logger.info(f"[INIT] Shared modules installed: {module_names}")

            # (2026-08-20 dedup) FRAMEWORK modules: app.providers.base + token_stats are now
            # delivered from SERVER and installed IN MEMORY only -- the CLIENT no longer ships
            # disk copies of its provider contract. On a fresh boot the bootstrap path has
            # already installed them (idempotent skip here); on re-init this is what installs them.
            framework_sources = result.get("framework_sources", {})
            if framework_sources:
                fw_success = self._loader.install_shared_modules(framework_sources, expected_hashes=shared_hashes or None)
                if not fw_success:
                    logger.warning("Failed to install some framework modules from SERVER")
                else:
                    fw_names = [k for k, v in framework_sources.items() if v]
                    logger.info(f"[INIT] Framework modules installed: {fw_names}")

            # SYSTEM PROMPT: the SERVER ships the finished prompts (tool turn + plain
            # chat). Stored in memory only; the CLIENT composes nothing.
            system_prompt = result.get("system_prompt", "")
            chat_prompt = result.get("chat_prompt", "")
            if system_prompt or chat_prompt:
                self._system_prompt = system_prompt
                self._chat_prompt = chat_prompt
                try:
                    from app.prompts import set_system_prompt_from_server
                    set_system_prompt_from_server(system_prompt, chat_prompt)
                except Exception as e:
                    logger.warning(f"[INIT] Failed to store delivered system prompt: {e}")
                logger.info(
                    f"[INIT] Prompts from SERVER: system={len(system_prompt)} chars, "
                    f"chat={len(chat_prompt)} chars"
                )

            # (2026-08-29 threadless refactor): boot path calls this directly, so it
            # must mark the orchestrator initialized itself — otherwise the first
            # get_ollama_tools() would re-fetch everything from the SERVER.
            self._initialized = True
            logger.info(f"Remote tools initialized: {len(definitions)} tools available")
            return len(definitions)

        except Exception as e:
            logger.error(f"Failed to process tools_response from SERVER: {e}", exc_info=True)
            return 0

    # --- Public interface (matches ToolOrchestrator) ---

    async def local_definitions(self, api_key: str = None) -> List[Dict]:
        """CLIENT-LOCAL definitions only (ask_user_question + MCP tools), role-filtered.

        No network I/O: the SERVER schema is already cached, so the ReAct loop can re-merge
        these mid-turn when an MCP server connects after the turn started. Remote names are
        excluded - a SERVER-delivered tool would shadow the local handler."""
        try:
            from tools.local_ask_registry import definitions as _local_definitions
            from tools.local_registry import definitions as _mcp_definitions
            from app.keys import get_key_role
            role = get_key_role(api_key) if api_key else None
            return list(_local_definitions(role)) + list(_mcp_definitions(role))
        except Exception as e:
            logger.debug(f"[LOCAL-TOOLS] definitions unavailable ({e}) - remote schema only")
            return []

    def local_tool_names(self) -> set:
        """Every name owned by a CLIENT-LOCAL seam, regardless of role or connectivity.

        The ReAct loop needs this to tell a local tool apart from a SERVER-delivered one when
        it re-merges the schema mid-turn: a local name that is no longer offered (server
        disconnected, role changed) must be REMOVED, not kept as if it were remote."""
        names: set = set()
        try:
            from tools.local_ask_registry import local_tool_names as _ask_names
            names.update(_ask_names())
        except Exception:
            pass
        try:
            from tools.local_registry import local_tool_names as _mcp_names
            names.update(_mcp_names())
        except Exception:
            pass
        return names

    async def get_ollama_tools(self, api_key: str = None) -> List[Dict]:
        """Return ONLY the definition dicts for the LLM - no extra fields.

            [{"type": "function", "function": {"name": "...", ...}}]

        Triggers lazy initialization on first call. CLIENT-LOCAL tools (ask_user_question) are
        merged in HERE - their handler owns a card and a Future on this machine - and are role
        filtered on the CLIENT because the SERVER never sees those names."""
        # Lazy init — fetch from SERVER on demand
        await self._ensure_initialized()

        defs = self._cache.get_definitions()

        if not defs:
            logger.warning("get_ollama_tools called but no definitions cached — SERVER may be unavailable")

        # Extract ONLY the definition field of each cached entry
        tool_defs = [d["definition"] if "definition" in d else d for d in defs]

        local = await self.local_definitions(api_key)
        if local:
            # A SERVER-delivered tool with the same name would shadow the local handler,
            # so local names win: the registry is the dispatcher, the schema is the promise.
            remote_names = {t.get("function", {}).get("name") for t in tool_defs}
            local = [d for d in local if d.get("function", {}).get("name") not in remote_names]
            tool_defs.extend(local)
            logger.info(f"get_ollama_tools: +{len(local)} CLIENT-local tool(s) "
                        f"({sorted(d.get('function', {}).get('name', '') for d in local)})")

        logger.info(f"get_ollama_tools: returning {len(tool_defs)} tool definitions")
        return tool_defs

    def get_tool_catalog(self) -> str:
        """Compact tool index for the agent prompt (SERVER catalog + CLIENT-local hints).

        Sync: the catalog was cached by install_from_response during lazy init, which the
        handler's starter_tools() call has already awaited before the turn runs. Returns ''
        when the SERVER shipped no catalog (empty tool list / legacy server)."""
        catalog = self._cache.get_catalog()
        return (catalog + _LOCAL_TOOL_HINTS) if catalog else ""

    async def starter_tools(self, api_key: str = None, tool_types: List[str] = None) -> List[Dict]:
        """Intent-gated per-turn schema: only the folders matching the user's intent + request_tools.

        Replaces get_ollama_tools() as the per-turn schema source. The model discovers the
        rest via the catalog and pulls it on demand with request_tools (see inject_tools).
        request_tools is ALWAYS present so escalation never depends on a schema the model
        does not already have.
        """
        await self._ensure_initialized()

        folders = set()
        for t in (tool_types or []):
            folders.update(_INTENT_CATEGORY_MAP.get(t, ()))

        categories = self._cache.get_categories()
        allowed = set(self._cache.get_allowed_tools() or [])
        defs = self._cache.get_definitions()

        out: List[Dict] = []
        for d in defs:
            name = _def_name(d)
            if allowed and name not in allowed:
                continue
            if categories.get(name) in folders:
                out.append(d["definition"] if "definition" in d else d)

        # request_tools always rides along (the escalation escape hatch).
        out.append(REQUEST_TOOLS_DEFINITION)

        logger.info(
            f"starter_tools: intent={list(tool_types or [])} -> {len(out)} tool(s) "
            f"({sorted(_def_name(d) for d in out)})"
        )
        return out

    async def inject_tools(self, current_schema: List[Dict], names: List[str],
                           api_key: str = None) -> List[Dict]:
        """Grow the schema to include the requested tools + their whole folder families.

        Resolves each requested name against the cached remote definitions (adding the
        tool's entire folder so one request pulls the neighbourhood) and the CLIENT-local
        seams. Returns the new complete schema (remote + local + request_tools). Idempotent:
        tools already present and request_tools are kept, never duplicated.
        """
        await self._ensure_initialized()

        categories = self._cache.get_categories()
        allowed = set(self._cache.get_allowed_tools() or [])
        defs = self._cache.get_definitions()

        requested = {n for n in (names or []) if isinstance(n, str) and n.strip()}
        folders_to_add = set()
        for d in defs:
            name = _def_name(d)
            if name in requested and categories.get(name):
                folders_to_add.add(categories[name])

        local = await self.local_definitions(api_key)
        local_by_name = {_def_name(d): d for d in local}

        existing = {_def_name(d) for d in (current_schema or [])}

        out: List[Dict] = []
        seen: set = set()

        def _add(d: Dict) -> None:
            name = _def_name(d)
            if name and name not in seen:
                seen.add(name)
                out.append(d)

        # Remote: keep already-granted + add the requested folders' tools (role-filtered).
        for d in defs:
            name = _def_name(d)
            if allowed and name not in allowed:
                continue
            if name in existing or categories.get(name) in folders_to_add:
                _add(d["definition"] if "definition" in d else d)

        # Local: keep already-granted + add explicitly requested local tools.
        for name, d in local_by_name.items():
            if name in existing or name in requested:
                _add(d)

        # request_tools always stays.
        _add(REQUEST_TOOLS_DEFINITION)

        added = sorted(seen - existing)
        logger.info(
            f"inject_tools: requested={sorted(requested)} -> schema {len(out)} tool(s) "
            f"(added: {added or 'none'})"
        )
        return out

    async def is_tool_allowed(self, tool_name: str, api_key: str = None) -> bool:
        """Check if a tool name exists in our registry.

        CLIENT-local tools count as allowed only when the role gate passes."""
        await self._ensure_initialized()  # Ensure tools are loaded first

        try:
            from tools.local_ask_registry import is_local_tool, allowed_for_role
            from app.keys import get_key_role
            if is_local_tool(tool_name):
                role = get_key_role(api_key) if api_key else None
                return allowed_for_role(tool_name, role)
        except Exception:
            pass

        # MCP names (the 4 meta-tools + dynamic mcp__<server>__<tool>) are gated here too.
        try:
            from tools.local_registry import is_local_tool as _mcp_is_local, allowed_for_role as _mcp_allowed
            from app.keys import get_key_role
            if _mcp_is_local(tool_name):
                return _mcp_allowed(tool_name, get_key_role(api_key) if api_key else None)
        except Exception:
            pass

        allowed = self._cache.get_allowed_tools()
        if allowed and tool_name not in allowed:
            return False
        return tool_name in self.tools or any(
            d.get("function", {}).get("name") == tool_name
            for d in self._cache.get_definitions()
        )

    async def get_tool_names(self, api_key: str = None) -> list:
        """Get all known tool names (remote + CLIENT-local)."""
        await self._ensure_initialized()  # Ensure tools are loaded first
        names = sorted(d.get("function", {}).get("name", "")
                       for d in self._cache.get_definitions())
        try:
            from tools.local_ask_registry import local_tool_names, allowed_for_role
            from app.keys import get_key_role
            role = get_key_role(api_key) if api_key else None
            names.extend(sorted(n for n in local_tool_names() if allowed_for_role(n, role)))
        except Exception:
            pass
        try:
            # MCP inventory: the 4 meta-tools plus the tools of every CONNECTED server,
            # each gated by that server's own role list (mcp_servers.json).
            from tools.local_registry import local_tool_names as _mcp_names, allowed_for_role as _mcp_allowed
            from app.keys import get_key_role
            role = get_key_role(api_key) if api_key else None
            names.extend(sorted(n for n in _mcp_names() if _mcp_allowed(n, role)))
        except Exception:
            pass
        return names

    async def get_tool_count(self, api_key: str = None) -> int:
        """Get the number of known tools."""
        await self._ensure_initialized()  # Ensure tools are loaded first
        return len(self._cache.get_definitions())

    # --- Tool loading & execution ---

    async def ensure_tool_loaded(self, tool_name: str) -> bool:
        """Ensure a tool is compiled and ready. Fetches from SERVER if needed."""
        # Already loaded?
        if self._cache.has_compiled(tool_name):
            func = self._cache.get_compiled(tool_name)
            self.tools[tool_name] = func
            return True

        # Source cached but not compiled? Pass shared_deps + config_constants.
        if self._cache.has_source(tool_name):
            source = self._cache.get_source_code(tool_name)
            shared_deps = self._cache.get_shared_deps(tool_name)  # FIX: retrieve deps from cache
            try:
                func = self._loader.compile_tool(
                    tool_name, source,
                    shared_deps=shared_deps,
                    config_constants=self._config_constants if self._config_constants else None,
                    source_hash=self._cache.get_source_hash(tool_name),
                    tool_manifest=self._cache.get_manifest(tool_name),
                )
                # FIX: Cache in BOTH loader internal dict AND ToolCache for consistency
                self._cache.set_compiled(tool_name, func)
                self.tools[tool_name] = func
                return True
            except ToolCompilationError as e:
                logger.error(f"Failed to compile cached tool {tool_name}: {e}")

                # FIX (2026-09-11, audit): the CACHED source can be STALE -- it was
                # delivered by an older SERVER build before this tool file was fixed on
                # disk (e.g. the 2026-09-10 connect.py that used `import socket` +
                # subprocess.run and failed the AST guard, so EVERY browser tool died).
                # The old behavior re-raised immediately: one bad delivery stuck for the
                # whole session even after the on-disk fix. Self-heal instead -- drop the
                # stale cache entries and fall through to a fresh fetch below (one-shot:
                # if the freshly fetched source also fails, that compile raises in the
                # fetch branch and propagates as before, so genuinely broken tools stay
                # NON-RETRYABLE exactly like today).
                self._cache.invalidate_source(tool_name)
                logger.warning(
                    f"Cached source for '{tool_name}' failed to compile -- "
                    "refetching fresh source from SERVER (stale-cache self-heal)"
                )

        # Need to fetch from SERVER
        if not self._fetcher or not self.provider:
            logger.error(f"Cannot load tool {tool_name} — no provider connected")
            return False

        try:
            result = await self._fetcher.fetch_tool_code(tool_name)

            if result is None or result.get("error"):
                err = result.get("error", "unknown error") if result else "no response"
                logger.error(f"SERVER denied/missing tool {tool_name}: {err}")
                return False

            source = result["source_code"]
            self._cache.set_source_code(tool_name, source)
            # (2026-08-20 fail-closed integrity): SERVER ships the SHA-256 prefix of this
            # exact source; compile_tool() refuses to run it if the bytes do not match.
            tool_source_hash = result.get("source_hash")
            if tool_source_hash:
                self._cache.set_source_hash(tool_name, tool_source_hash)

            # FIX (2026-01): Cache shared_deps from SERVER and pass them to compile_tool()
            server_shared_deps = result.get("shared_deps")
            if server_shared_deps:
                self._cache.set_shared_deps(tool_name, server_shared_deps)
                logger.info(f"[INIT] Cached {len(server_shared_deps)} shared deps for tool '{tool_name}'")

            # Self-unpacking tools (2026-09-11): cache the SERVER-shipped manifest so both
            # compile paths (fresh + cached-source) inject it as TOOL_MANIFEST.
            tool_manifest = result.get("manifest")
            if tool_manifest:
                self._cache.set_manifest(tool_name, tool_manifest)

            func = self._loader.compile_tool(
                tool_name, source,
                shared_deps=server_shared_deps,
                config_constants=self._config_constants if self._config_constants else None,
                source_hash=tool_source_hash,
                tool_manifest=tool_manifest,
            )
            # FIX: Cache in BOTH loader internal dict AND ToolCache for consistency
            self._cache.set_compiled(tool_name, func)
            self.tools[tool_name] = func
            logger.info(f"Tool {tool_name} loaded and compiled successfully")
            return True

        except ToolCompilationError:
            # Re-raise compilation errors so they're NOT retried by retry logic
            raise
        except Exception as e:
            logger.error(f"Failed to load tool {tool_name} from SERVER: {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # OUT-OF-SANDBOX NETWORK CONSENT context bridge (2026-11-24)
    # ------------------------------------------------------------------

    @staticmethod
    def _call_pyexec_with_context(func, arguments, websocket=None, conversation_id: str = None,
                                  loop=None, cycle=None):
        """Run a python_exec tool call with the chat's relay context registered.

        tools.exec_tools.python_exec reads it (threading.local on a sys.modules anchor) to relay
        out-of-sandbox socket attempts to the UI approval card. Set/restored on THIS thread
        sync calls run via asyncio.to_thread, so chats never cross wires; *loop* must be captured
        on the event-loop thread first. Missing args -> no context. *cycle* is this turn's
        CycleRecorder, so relay cards become its rows.
        """
        import sys as _sys, types as _types, threading as _threading

        key = "_coolems_pyexec_context"
        mod = _sys.modules.get(key)
        if mod is None:
            mod = _types.ModuleType(key)
            mod.context_local = _threading.local()
            _sys.modules[key] = mod
        local = getattr(mod, "context_local", None)
        if websocket is not None and conversation_id and loop is not None:
            local.context = {"websocket": websocket,
                             "conversation_id": conversation_id,
                             "loop": loop,
                             "cycle": cycle}
        else:
            local.context = None
        try:
            return func(**arguments)
        finally:
            local.context = None

    @staticmethod
    def _call_desktop_with_context(func, arguments, conversation_id: str = None):
        """Run a Computer Use (cua_*) tool call with the chat's desktop consent channel.

        tools/computer_use/_consent.py reads it (threading.local on 'tools._consent') to decide
        which conversation may use which app. Set on the worker thread, cleared in finally, so
        concurrent chats never share grants; no conversation id -> fail closed."""
        import sys as _sys

        consent = _sys.modules.get("tools._consent")
        if consent is None or not conversation_id:
            return func(**arguments)
        try:
            consent.register_context(conversation_id)
            return func(**arguments)
        finally:
            try:
                consent.clear_context()
            except Exception:
                pass

    async def execute_tool(
    self,
    tool_name: str,
    arguments: Dict[str, Any],
    conversation_history: list = None,
    api_key: str = None,
    websocket=None,
    conversation_id: str = None,
    cycle=None
    ) -> Any:
        """Execute a tool by name with the given arguments. Fetches if needed.

        Signature matches ToolOrchestrator; extra kwargs (conversation_history, api_key) are
        accepted and unused. ToolCompilationError is NOT caught - it ends the loop as a
        non-retryable error.

        Context kwargs: websocket / conversation_id (python_exec stores them on a threading.local
        anchor so the sandboxed child can relay consent to this chat; desktop tools carry the id
        on 'tools._consent' to verify the per-app grant) and cycle (the turn's CycleRecorder)."""
        # Ensure tool is loaded (may raise ToolCompilationError)
        await self.ensure_tool_loaded(tool_name)

        func = self.tools.get(tool_name)
        if not func:
            logger.error(f"Tool '{tool_name}' not found in registry after ensure_tool_loaded")
            return f"Error: Tool '{tool_name}' is not available."

        try:
            # SECURITY (2026-07-15): the model must NEVER be able to set or
            # override 'working_root'/'work_folder'. The real root comes from the
            # CLIENT's own config (injected into shared utils at install time).
            # Strip any model-supplied values before calling the tool.
            arguments = dict(arguments)
            for _p in ('working_root', 'work_folder'):
                if _p in arguments:
                    logger.warning(
                        f"[SECURITY] Stripped model-supplied '{_p}' from tool "
                        f"'{tool_name}' arguments -- system working_root always wins."
                    )
                    del arguments[_p]

            # Call the tool function with arguments.
            #
            # FIX (2026-08-29): sync tools MUST run OFF the event loop. The old code
            # called func() directly on uvicorn's asyncio thread, so any blocking work
            # inside a tool (python_exec's subprocess.run up to 300s, image generation,
            # heavy disk I/O) froze the ENTIRE HTTP server for the whole duration -
            # the UI's /api/tree fetch then failed and the Files tab showed
            # "Error loading files" until the tool finished. asyncio.to_thread() moves
            # blocking calls to a worker thread; coroutines are still awaited in place.
            if inspect.iscoroutinefunction(func):
                result = await func(**arguments)
            elif tool_name == "python_exec":
                # OUT-OF-SANDBOX NETWORK CONSENT (2026-11-24): register this
                # chat's relay context for the duration of the call so the
                # sandbox can ask the user about external connections.
                # Capture the running loop HERE (on the event-loop thread): the
                # helper itself runs in a worker thread and cannot see it.
                _net_loop = asyncio.get_running_loop()
                result = await asyncio.to_thread(
                    self._call_pyexec_with_context, func, arguments,
                    websocket, conversation_id, _net_loop, cycle)
            elif tool_name.startswith("cua_"):
                # COMPUTER USE (2026-11-07): the per-app grant lives on the
                # tools._consent channel, keyed by conversation id. Set it for the
                # duration of the call so the tool can verify the grant itself.
                result = await asyncio.to_thread(
                    self._call_desktop_with_context, func, arguments, conversation_id)
            else:
                result = await asyncio.to_thread(func, **arguments)

            # Handle async functions (defensive - covers manually returned coroutine objects)
            if asyncio.iscoroutine(result):
                result = await result

            logger.debug(f"Tool {tool_name} executed successfully")
            return result

        except Exception as e:
            logger.error(f"Tool execution failed for {tool_name}: {e}")
            return f"Tool execution error: {e}"

    # --- Utility methods ---

    def get_tools_summary(self) -> Dict[str, Any]:
        """Get a summary of the current state."""
        return {
            "initialized": self._initialized,
            "cached_definitions": len(self._cache.get_definitions()),
            "cached_sources": sum(1 for _ in self._cache._source_code),
            "compiled_tools": len(self.tools),
            "allowed_tools": self._cache.get_allowed_tools(),
            "config_constants_count": len(self._config_constants),
        }

    def get_system_prompt(self) -> str:
        """Tool-turn system prompt received from SERVER ('' when not delivered)."""
        return self._system_prompt

    def get_chat_prompt(self) -> str:
        """Plain-chat prompt received from SERVER ('' when not delivered)."""
        return self._chat_prompt

    def clear_tools(self):
        """Clear all cached tools (called on disconnect).

        The working root needs no clearing - it is never stored here; each turn's value comes
        from the conversation's DB row via agentic_mode()."""
        self._initialized = False
        # (2026-08-29 threadless refactor): cancel an in-flight lazy-init task, if any —
        # after a full reset the next metadata call must start a fresh fetch.
        if self._init_task is not None and not self._init_task.done():
            self._init_task.cancel()
            self._init_task = None
        self._cache.clear_all()
        self.tools.clear()
        self._config_constants.clear()
        self._system_prompt = ""  # Clear prompts from SERVER too
        self._chat_prompt = ""

        logger.info("RemoteToolOrchestrator: all tools cleared")
