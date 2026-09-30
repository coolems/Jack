"""In-memory tool cache — stores definitions, source code, and compiled modules."""



import logging

from typing import Dict, Optional, Any, List



logger = logging.getLogger("COOLEMS.Tools.Remote.Cache")





class ToolCache:

    """Pure in-memory LRU-style cache for remote tools. No disk persistence.



    Three layers:

      1. definitions   — __tool_description__ dicts sent to LLM (cached on connect)

      2. source_code   — raw .py content from SERVER (fetched on demand)

      3. compiled      — live callable function objects (compiled from source)

    """



    def __init__(self, max_entries: int = 100):
        """Create an empty cache. *max_entries* bounds the source/compiled layers (LRU eviction)."""
        self.max_entries = max_entries
        self._definitions: List[Dict] = []
        self._allowed_tools: Optional[List[str]] = None
        self._source_code: Dict[str, str] = {}
        self._shared_deps: Dict[str, Dict[str, str]] = {}  # tool_name -> {dep_name: source}
        self._compiled: Dict[str, Any] = {}  # name -> callable function

        # Shared modules (utils.py, path_guard.py, etc.) sent from SERVER as a dict
        self._shared_sources: Dict[str, Optional[str]] = {}

        # (2026-08-20 fail-closed integrity): SERVER-provided SHA-256 prefixes per tool source.
        # compile_tool() refuses to run a cached source whose hash does not match.
        self._source_hashes: Dict[str, str] = {}

        # Self-unpacking tools (2026-09-11): SERVER-shipped tool manifests (client-side
        # runtime bootstrap config for self-expanding tools). Keyed by tool name; dropped
        # together with the source so a stale manifest can never outlive its source.
        self._manifests: Dict[str, dict] = {}



    # --- Definitions (fetched once on connect) ---

    def set_definitions(self, definitions: List[Dict], allowed_tools: Optional[List[str]] = None):
        """Store tool definitions received from SERVER."""

        self._definitions = definitions
        self._allowed_tools = allowed_tools

        logger.info(f"Cache: {len(definitions)} definitions stored, allowed={allowed_tools}")



    def get_definitions(self) -> List[Dict]:
        """Return a copy of the tool definition dicts (safe to iterate/mutate)."""
        return list(self._definitions)



    def get_allowed_tools(self) -> Optional[List[str]]:
        """Role-filtered tool names from the SERVER (None = no explicit filter)."""
        return self._allowed_tools



    # --- Shared modules (fetched once on connect) ---

    def set_shared_sources(self, sources: Dict[str, str]):
        """Store shared module source codes received from SERVER."""

        self._shared_sources = dict(sources) if sources else {}

        names = [k for k, v in self._shared_sources.items() if v]

        logger.info(f"Cache: {len(names)} shared modules stored: {names}")



    def get_shared_source(self, module_name: str) -> Optional[str]:
        """Get source code for a single shared module by name."""
        return self._shared_sources.get(module_name)



    def get_all_shared_sources(self) -> Dict[str, str]:
        """Return all non-None shared sources as a dict."""
        return {k: v for k, v in self._shared_sources.items() if v}



    # LEGACY compatibility — some callers still use the old single-string API

    def set_shared_utils_source(self, source: Optional[str]):
        """Legacy alias for backward compat. Stores under 'utils' key."""
        if source:

            self._shared_sources['utils'] = source

            logger.info(f"Cache: shared utils stored ({len(source)} chars)")



    def get_shared_utils_source(self) -> Optional[str]:
        """Legacy alias for backward compat. Returns 'utils' source."""
        return self._shared_sources.get('utils')



    # --- Shared deps (same-package dependencies for tools) ---

    def has_shared_deps(self, name: str) -> bool:
        """True if same-package dependency sources are stored for *name*."""
        return name in self._shared_deps



    def set_shared_deps(self, name: str, deps: Dict[str, str]):
        """Store same-package dependencies for a tool."""

        self._shared_deps[name] = deps

        logger.debug(f"Cache: {len(deps)} shared deps stored for '{name}'")



    def get_shared_deps(self, name: str) -> Optional[Dict[str, str]]:
        """Dependency sources for a tool ({module_name: source}), or None."""
        return self._shared_deps.get(name)



    # --- Tool manifests (self-unpacking tools, 2026-09-11) ---

    def set_manifest(self, name: str, manifest: dict):
        """Store the SERVER-shipped tool manifest for *name* (client-side runtime config)."""
        if isinstance(manifest, dict) and manifest:
            self._manifests[name] = manifest
            logger.debug(f"Cache: manifest stored for '{name}'")

    def get_manifest(self, name: str) -> Optional[dict]:
        """SERVER-shipped tool manifest for *name*, or None (legacy server / no manifest)."""
        return self._manifests.get(name)

    # --- Source code (fetched on demand, cached forever in session) ---

    def has_source(self, name: str) -> bool:
        """True if the raw source for *name* is already cached."""
        return name in self._source_code



    def set_source_code(self, name: str, source: str):
        """Store raw tool source; evicts the oldest entry when full and invalidates its compiled copy."""

        if len(self._source_code) >= self.max_entries:

            logger.warning(f"Cache full ({self.max_entries}), evicting oldest entry")

            oldest = next(iter(self._source_code))

            del self._source_code[oldest]

            self._compiled.pop(oldest, None)

            self._source_hashes.pop(oldest, None)



        self._source_code[name] = source

        # Invalidate compiled version when new source arrives

        self._compiled.pop(name, None)



    def get_source_code(self, name: str) -> Optional[str]:
        """Cached raw source for *name*, or None when not fetched yet."""
        return self._source_code.get(name)


    def invalidate_source(self, name: str):
        """Drop cached source (+ hash + compiled copy) for *name* so the next load

        re-fetches fresh code from the SERVER (2026-09-11 audit fix). Used when a
        CACHED source fails to compile: it may be stale -- delivered by an older
        SERVER build before this tool file was fixed on disk. The compiled entry is
        dropped too, so no half-built callable survives the invalidation.
        """
        if name in self._source_code or name in self._compiled:
            self._source_code.pop(name, None)
            self._source_hashes.pop(name, None)
            self._shared_deps.pop(name, None)
            self._manifests.pop(name, None)
            self._compiled.pop(name, None)
            logger.info(f"Cache: invalidated stale source for '{name}' -- next load refetches from SERVER")

    # --- Source integrity hashes (2026-08-20 fail-closed) ---

    def set_source_hash(self, name: str, sha_prefix: str):
        """Store the SERVER-provided SHA-256 prefix used to verify cached sources."""
        if sha_prefix:

            self._source_hashes[name] = sha_prefix



    def get_source_hash(self, name: str) -> Optional[str]:
        """Stored hash prefix for *name*, or None (unverified/legacy)."""
        return self._source_hashes.get(name)



    # --- Compiled functions (built from source + shared utils) ---

    def has_compiled(self, name: str) -> bool:
        """True if a live callable for *name* is already built."""
        return name in self._compiled



    def set_compiled(self, name: str, func: Any):
        """Store a compiled callable; evicts the oldest entry when full."""

        if len(self._compiled) >= self.max_entries:

            oldest = next(iter(self._compiled))

            del self._compiled[oldest]



        self._compiled[name] = func



    def get_compiled(self, name: str) -> Optional[Any]:
        """Compiled callable for *name*, or None when not built yet."""
        return self._compiled.get(name)



    # --- Utility ---

    def clear_all(self):
        """Clear everything (called on disconnect)."""

        self._definitions.clear()

        self._allowed_tools = None

        self._source_code.clear()

        self._shared_deps.clear()

        self._compiled.clear()

        self._shared_sources.clear()

        self._source_hashes.clear()
        self._manifests.clear()

        logger.info("Cache: cleared all entries")



    def summary(self) -> Dict:
        """Compact cache statistics dict (used in logs/debug endpoints)."""
        return {

            "definitions": len(self._definitions),

            "source_entries": len(self._source_code),

            "compiled_entries": len(self._compiled),

            "allowed_tools": self._allowed_tools,

            "shared_modules": list(k for k, v in self._shared_sources.items() if v),
            "manifest_entries": len(self._manifests),

        }
