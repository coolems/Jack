"""RemoteToolOrchestrator - drop-in replacement for ToolOrchestrator.

All tool source code is fetched from SERVER on-demand via WebSocket.
CLIENT starts with ZERO tools and builds them dynamically in memory.

Same public interface (get_ollama_tools, execute_tool, etc.) but all tool
source code comes from SERVER on-demand. All execution happens locally.

Workflow:
  1. First call to get_ollama_tools() triggers lazy fetch from SERVER
  2. When LLM needs tool X: request source code from SERVER
  3. Compile source into live function with working_root injected
  4. Execute locally exactly like current ToolOrchestrator does

FIXED (2026-08):
  - Now installs ALL shared modules (utils, path_guard, etc.) not just utils.py
  - Creates proper 'tools' namespace package in sys.modules for import resolution

FIXED (2026-01):
  - Passes shared_deps from SERVER to compile_tool() so sibling modules
    (shared_instance, ensure_connected) are available during compilation.

FIXED (2026-01-31):
  - Stores and passes config_constants from SERVER to compile_tool() so that
    tools like web_search have access to WEB_SEARCH_* constants at runtime.
"""

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional

from .tool_cache import ToolCache
from .tool_fetcher import ToolFetcher
from .dynamic_loader import DynamicModuleLoader, ToolCompilationError

logger = logging.getLogger("COOLEMS.Tools.Remote.Orchestrator")


class RemoteToolOrchestrator:
    """Remote tool orchestrator - fetches tools from SERVER on-demand.

    Uses LAZY initialization - tools are fetched from SERVER only when first needed.
    This avoids event loop conflicts during startup and ensures tools are always available.
    """

    def __init__(self, provider=None, ollama_url: str = None):
        self.provider = provider
        self.ollama_url = ollama_url

        # Core components
        self._cache = ToolCache()
        self._fetcher = ToolFetcher(provider) if provider else None
        self._loader = DynamicModuleLoader()  # working root is resolved live from CLIENT/config/.working_root.json

        # Runtime tool registry (name -> callable)
        self.tools: Dict[str, Callable] = {}

        # Config constants from SERVER (shared across ALL tools)
        self._config_constants: Dict[str, Any] = {}

        # System prompt from SERVER — stored in memory only, never saved to disk
        self._system_prompt: str = ""

        # Initialization tracking (2026-08-29 threadless refactor): no
        # threading.Lock and no executor. Everything runs on the single event
        # loop, so overlapping callers are deduplicated by sharing one in-flight
        # asyncio task instead of locking a worker thread.
        self._initialized = False
        self._init_task: Optional[asyncio.Task] = None

        logger.info("RemoteToolOrchestrator initialized (zero tools — will fetch from SERVER on demand)")

    # NOTE: no set_working_root() here on purpose - the working root lives ONLY in
    # <CLIENT>/config/.working_root.json (single source of truth). Tools read that file
    # live via tools.utils.get_working_root(), so nothing has to be propagated.

    # --- Lazy initialization (called automatically when first needed) ---

    async def _ensure_initialized(self) -> bool:
        """Fetch tools from SERVER if not already done — single-loop, no threads.

        2026-08-29 threadless refactor: the old implementation ran
        asyncio's run() helper inside a one-shot worker thread, guarded by an OS lock.
        That spawned an OS worker thread + a second event loop for what is a
        plain async network fetch that can simply be awaited on the running
        loop. Concurrency is now handled by the single-loop nature of asyncio:
        one shared in-flight task dedupes overlapping callers (they all await
        the same task), and there is nothing to lock because only the event
        loop thread ever touches this state.
        """
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
        """Fetch allowed tool definitions + shared modules from SERVER.

        Returns number of tools available. Awaited directly on the running event loop
        by _ensure_initialized() (2026-08-29: no thread pool / asyncio.run).
        The network fetch is followed by install_from_response() which does all the
        bookkeeping (definitions, shared modules, DNA data, config constants, prompt).
        """
        if not self._fetcher or not self.provider:
            logger.error("Cannot initialize — no provider connected")
            return 0

        try:
            result = await self._fetcher.fetch_tools()
        except Exception as e:
            logger.error(f"[ERROR] Failed to fetch tool definitions from SERVER: {e}", exc_info=True)
            return 0

        if result is None:
            logger.error("[ERROR] fetch_tools returned None — SERVER did not respond")
            return 0

        try:
            return self.install_from_response(result)
        except Exception as e:
            logger.error(f"[ERROR] Failed to process tools_response from SERVER: {e}", exc_info=True)
            return 0

    def install_from_response(self, result: Dict[str, Any]) -> int:
        """Install a SERVER tools_response payload (NO network I/O).

        Split out of initialize_from_server() on 2026-08-21 so the boot bootstrap
        path can reuse it: app/providers/bootstrap.py fetches this same response
        BEFORE CoolemsClientProvider exists (it subclasses BaseProvider, which only
        becomes importable after framework_sources are installed). Returns number of
        tools available.

        NOTE: install_shared_modules() is idempotent — modules the bootstrap already
        put in sys.modules (app.providers.base/token_stats) are skipped here.
        """
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
                logger.warning("[WARN] SERVER sent empty tool list (no tools available)")
                return 0

            # Store in cache
            self._cache.set_definitions(definitions, allowed_tools)

            # FIX (2026-08-17): DNA data files from SERVER -- synced into the CLIENT's own
            # app/dna directory (NEVER working_root) so delivered app.dna.* modules read them there.
            dna_data = result.get("dna_data", {})
            if dna_data:
                self._loader.set_pending_dna_data(dna_data)

            # FIX (2026-07-18): give the loader the SERVER config constants BEFORE installing
            # shared modules so delivered code with module-level 'from config import X' lines
            # (e.g. app.dna.learning -> MODEL_NAME) can be rewritten to real assignments.
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
                    logger.warning("[WARN] Failed to install some shared modules from SERVER")
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
                    logger.warning("[WARN] Failed to install some framework modules from SERVER")
                else:
                    fw_names = [k for k, v in framework_sources.items() if v]
                    logger.info(f"[INIT] Framework modules installed (in-memory dedup): {fw_names}")

            # SYSTEM PROMPT: Extract and store in memory only (never save to disk)
            system_prompt = result.get("system_prompt", "")
            if system_prompt:
                self._system_prompt = system_prompt
                logger.info(f"[INIT] System prompt received from SERVER ({len(system_prompt)} chars)")
                # Also update the module-level GENERIC_TOOL_PROMPT so existing imports work
                try:
                    from app.prompts import set_system_prompt_from_server
                    set_system_prompt_from_server(system_prompt)
                    logger.info("[INIT] Module-level GENERIC_TOOL_PROMPT updated from SERVER")
                except Exception as e:
                    logger.warning(f"[INIT] Failed to update module-level prompt: {e}")

            # (2026-08-29 threadless refactor): boot path calls this directly, so it
            # must mark the orchestrator initialized itself — otherwise the first
            # get_ollama_tools() would re-fetch everything from the SERVER.
            self._initialized = True
            logger.info(f"Remote tools initialized: {len(definitions)} tools available from SERVER")
            return len(definitions)

        except Exception as e:
            logger.error(f"[ERROR] Failed to process tools_response from SERVER: {e}", exc_info=True)
            return 0

    # --- Public interface (matches ToolOrchestrator) ---

    async def get_ollama_tools(self, api_key: str = None) -> List[Dict]:
        """Return ONLY the definition dicts for LLM — no extra fields.

        llama.cpp expects standard OpenAI-style tool definitions:
            [{"type": "function", "function": {"name": "...", ...}}, ...]

        TRIGGERS LAZY INITIALIZATION on first call if tools haven't been fetched yet.
        """
        # Lazy init — fetch from SERVER if not already done
        await self._ensure_initialized()

        defs = self._cache.get_definitions()

        if not defs:
            logger.warning("get_ollama_tools called but no definitions cached — SERVER may be unavailable")
            return []

        # Extract ONLY the definition field from each cached entry
        tool_defs = [d["definition"] if "definition" in d else d for d in defs]
        logger.info(f"get_ollama_tools: returning {len(tool_defs)} remote tool definitions")
        return tool_defs

    async def is_tool_allowed(self, tool_name: str, api_key: str = None) -> bool:
        """Check if a tool name exists in our registry."""
        await self._ensure_initialized()  # Ensure tools are loaded first

        allowed = self._cache.get_allowed_tools()
        if allowed and tool_name not in allowed:
            return False
        return tool_name in self.tools or any(
            d.get("function", {}).get("name") == tool_name
            for d in self._cache.get_definitions()
        )

    async def get_tool_names(self, api_key: str = None) -> list:
        """Return all known tool names."""
        await self._ensure_initialized()  # Ensure tools are loaded first
        return sorted(d.get("function", {}).get("name", "")
                      for d in self._cache.get_definitions())

    async def get_tool_count(self, api_key: str = None) -> int:
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
                logger.error(f"[ERROR] Failed to compile cached tool {tool_name}: {e}")

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
                    f"[WARN] Cached source for '{tool_name}' failed to compile -- "
                    "refetching fresh source from SERVER (stale-cache self-heal)"
                )

        # Need to fetch from SERVER
        if not self._fetcher or not self.provider:
            logger.error(f"[ERROR] Cannot load tool {tool_name} — no provider connected")
            return False

        try:
            result = await self._fetcher.fetch_tool_code(tool_name)

            if result is None or result.get("error"):
                err = result.get("error", "unknown error") if result else "no response"
                logger.error(f"[ERROR] SERVER denied/missing tool {tool_name}: {err}")
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
            logger.error(f"[ERROR] Failed to load tool {tool_name} from SERVER: {e}", exc_info=True)
            return False

    async def execute_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        conversation_history: list = None,
        api_key: str = None
    ) -> Any:
        """Execute a tool by name with the given arguments. Fetches if needed.

        Signature matches old ToolOrchestrator for drop-in compatibility.
        Extra kwargs (conversation_history, api_key) are accepted but not used.

        NOTE: ToolCompilationError exceptions propagate through here and are
              NOT caught — they bubble up to react_loop which treats them as
              non-retryable errors that should terminate the agentic loop immediately.
        """
        # Ensure tool is loaded (may raise ToolCompilationError)
        await self.ensure_tool_loaded(tool_name)

        func = self.tools.get(tool_name)
        if not func:
            logger.error(f"[ERROR] Tool '{tool_name}' not found in registry after ensure_tool_loaded")
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
            else:
                result = await asyncio.to_thread(func, **arguments)

            # Handle async functions (defensive - covers manually returned coroutine objects)
            if asyncio.iscoroutine(result):
                result = await result

            logger.debug(f"[DEBUG] Tool {tool_name} executed successfully")
            return result

        except Exception as e:
            logger.error(f"[ERROR] Tool execution failed for {tool_name}: {e}")
            return f"Tool execution error: {e}"

    # --- Utility methods ---

    def get_tools_summary(self) -> Dict[str, Any]:
        """Return a summary of tool state."""
        return {
            "initialized": self._initialized,
            "cached_definitions": len(self._cache.get_definitions()),
            "cached_sources": sum(1 for _ in self._cache._source_code),
            "compiled_tools": len(self.tools),
            "allowed_tools": self._cache.get_allowed_tools(),
            "config_constants_count": len(self._config_constants),
        }

    def get_system_prompt(self) -> str:
        """Return the system prompt received from SERVER (memory only).

        Returns empty string if not yet delivered.
        """
        return self._system_prompt

    def clear_tools(self):
        """Clear all cached tools (called on disconnect).

        The working root needs no clearing here - it is not stored in this process
        at all; every session reads <CLIENT>/config/.working_root.json live.
        """
        self._initialized = False
        # (2026-08-29 threadless refactor): cancel an in-flight lazy-init task, if any —
        # after a full reset the next metadata call must start a fresh fetch.
        if self._init_task is not None and not self._init_task.done():
            self._init_task.cancel()
            self._init_task = None
        self._cache.clear_all()
        self.tools.clear()
        self._config_constants.clear()
        self._system_prompt = ""  # Clear system prompt from memory too

        logger.info("RemoteToolOrchestrator: all tools cleared")
