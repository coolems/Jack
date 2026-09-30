"""DynamicModuleLoader -- the public facade for loading SERVER-delivered tool code.

This class owns ALL loader state (installed-shared-modules flag, shared sources kept
for re-install, config constants, integrity hashes) and delegates each concern to a
focused module in this package:

  errors           ToolCompilationError (+ subclasses)   -- non-retryable compile failures
  policy           per-tool import/call allowlists       -- the auditable trust table
  ast_guard        AST security validation               -- runs BEFORE anything is exec'd
  integrity        fail-closed SHA-256 prefix checks     -- promised hash mismatch => refuse
  source_transform tool-source rewrite pipeline          -- imports -> injected-symbol refs
  shared_modules   sys.modules installation of shared code (utils, path_guard, app.dna.*)
  globals_builder  exec-globals assembly + shared_deps install

Behavior is byte-for-byte the same as the original single-file dynamic_loader.py:
same public methods, same error messages, same fail-closed semantics. The split exists
for maintenance/debugging -- each concern can now be read, tested and changed alone.
"""

import logging
from typing import Any, Callable, Dict, Optional

from .errors import ToolCompilationError
from .ast_guard import validate_source_code
from .integrity import hash_in_allowlist, verify_tool_hash
from .source_transform import transform_tool_source
from .shared_modules import install_shared_modules as _install_shared_modules_impl
from .globals_builder import (
    build_tool_exec_globals,
    extract_tool_function,
    inject_config_constants,
    install_shared_deps,
    _same_package_dep_names,
)

logger = logging.getLogger(__name__)


class DynamicModuleLoader:
    """Loads and compiles remote tool modules with security checks.

    Typical lifecycle (driven by RemoteToolOrchestrator):

        loader.set_config_constants(result["config_constants"])
        loader.set_expected_shared_hashes(result["shared_hashes"])
        loader.install_shared_modules(result["shared_sources"], expected_hashes=...)
        func = loader.compile_tool(name, source, shared_deps=..., config_constants=...)
    """

    def __init__(self):
        """Initialize the dynamic module loader.

        The working root is NOT stored here - tools resolve it live from
        <CLIENT>/config/.working_root.json (single source of truth).
        """
        self._compiled_tools: Dict[str, Callable[..., Any]] = {}
        self._modules_installed = False
        self._shared_sources: Dict[str, str] = {}
        self._allowed_hashes: set[str] = set()
        # (2026-08-20 fail-closed integrity): per-module SHA-256 prefixes shipped by the
        # SERVER in tools_response 'shared_hashes'. When present, a module whose delivered
        # source does NOT match is refused (skipped) instead of exec'd with a warning.
        self._expected_shared_hashes: Dict[str, str] = {}
        # FIX (2026-07-18): config constants from SERVER -- used to rewrite 'from config import X'
        # lines in delivered shared modules at install time (e.g. app.dna.learning's MODEL_NAME).
        self._config_constants: Dict[str, Any] = {}

    # ------------------------------------------------------------------ state

    def set_config_constants(self, constants: dict) -> None:
        """Store SERVER-provided config constants for shared-module installation.

        Called by RemoteToolOrchestrator BEFORE install_shared_modules() so that
        delivered modules referencing config names at module level can exec cleanly.
        """
        self._config_constants = dict(constants) if constants else {}

    def set_expected_shared_hashes(self, hashes: dict) -> None:
        """Store SERVER-shipped SHA-256 prefixes for shared/framework modules.

        Called by RemoteToolOrchestrator BEFORE install_shared_modules(). Fail-closed:
        when a hash is present and the delivered source does not match, that module is
        NOT exec'd (2026-08-20). Absent hashes are tolerated (legacy server).
        """
        self._expected_shared_hashes = dict(hashes) if hashes else {}

    def set_pending_dna_data(self, dna_data: dict) -> None:
        """Store DNA JSON data files from the SERVER for syncing on next install.

        Called by RemoteToolOrchestrator with tools_response 'dna_data' before
        install_shared_modules(). Files are synced into the CLIENT's own app/dna
        directory (NEVER working_root) so the delivered app.dna.* modules find them there.
        """
        self._pending_dna_data = dict(dna_data) if dna_data else None

    def set_allowed_hashes(self, hashes: set[str]) -> None:
        """Set allowed SHA-256 hash prefixes for tool verification (legacy advisory path)."""
        self._allowed_hashes = set(hashes)

    def clear_allowed_hashes(self) -> None:
        """Clear the hash allowlist (allow all tools)."""
        self._allowed_hashes = set()

    # ------------------------------------------------------------- installation

    # LEGACY compatibility -- old API accepted a single utils source string.
    def install_shared_utils(self, source_code: str) -> bool:
        """Legacy alias for backward compat. Wraps into dict and calls install_shared_modules."""
        return self.install_shared_modules({'utils': source_code})

    def install_shared_modules(self, sources: Dict[str, str], expected_hashes: Optional[Dict[str, str]] = None) -> bool:
        """Install shared utility modules for remote tool execution.

        Creates proper namespace package structure in sys.modules so that Python's
        import machinery can resolve 'from tools.utils import X' etc.

        CRITICAL FIX (2026-01): Now handles subpackage __init__.py files properly.
        When a key like 'web_interact' is provided, it creates the parent package
        BEFORE processing child modules (e.g. 'web_interact.shared_instance').
        This allows relative imports within subpackages to work correctly.

        CRITICAL FIX (2026-01-31): Fixed subpackage module attachment so that
        nested packages are set as attributes on their parent package, not just
        the top-level tools namespace. This allows Python to resolve
        'import tools.web_interact' and 'from tools.web_interact import X'.

        Args:
            sources: Dict mapping module names to source code strings.
                     Keys are like 'utils', 'path_guard', 'ssrf_defense'.
                     Subpackage keys use dot notation: 'web_interact', 'web_interact.shared_instance'.
                     Values are the raw Python source text (can be None if not available).

        Returns:
            True if installation succeeded, False otherwise.
        """
        # Per-call hashes win over stored ones; fall back to set_expected_shared_hashes().
        effective_hashes = expected_hashes or self._expected_shared_hashes or {}

        success = _install_shared_modules_impl(
            sources,
            expected_hashes=effective_hashes,
            config_constants=self._config_constants,
            dna_data=getattr(self, '_pending_dna_data', None),
        )

        if success:
            self._modules_installed = True
            # Store sources for potential re-install after clear.
            self._shared_sources = dict(sources)
            # DNA data was consumed by the install -- drop it so a later re-install
            # (auto-retry in compile_tool) does not rewrite the files again.
            if getattr(self, '_pending_dna_data', None):
                self._pending_dna_data = None

        return success

    # ------------------------------------------------------------------ compile

    def compile_tool(self, tool_name: str, source_code: str, shared_deps: dict = None,
                     config_constants: dict = None, source_hash: Optional[str] = None,
                     tool_manifest: Optional[dict] = None) -> Callable[..., Any]:
        """Compile a single tool from source code and return the callable function.

        Pipeline (each step isolated in its own module for debugging):
          1. AST security validation            (ast_guard.validate_source_code)
          2. fail-closed integrity checks       (integrity.verify_tool_hash + allowlist)
          3. shared modules auto-install if needed
          4. source transformation              (source_transform.transform_tool_source)
          5. exec-globals assembly              (globals_builder.build_tool_exec_globals)
          6. config constants injection         (globals_builder.inject_config_constants)
          6b. tool manifest injection            (SERVER-shipped TOOL_MANIFEST global)
          7. shared deps install + symbol merge (globals_builder.install_shared_deps)
             + REQUIRED-DEP GATE: a same-package import whose dep failed to install
               raises ToolCompilationError here instead of a runtime NameError later
               (2026-09-20 fail-loud fix -- the python_exec SANDBOX_BOOTSTRAP incident)
          8. exec transformed source
          9. extract the tool callable          (must be named exactly *tool_name*)

        Args:
            tool_name: Name of the tool (used for module name, allowlists and logging).
            source_code: Python source code containing the tool function.
            shared_deps: Optional dict mapping dep_name -> source_code for same-package dependencies.
            config_constants: Optional dict of config constants to inject into exec_globals
                              (e.g. WEB_SEARCH_DEFAULT_MAX_RESULTS, WEB_SEARCH_SEARXNG_INSTANCES).
            source_hash: Optional SERVER-shipped SHA-256 prefix; a mismatch REFUSES compilation
                         (fail-closed, 2026-08-20).
             tool_manifest: Optional SERVER-shipped tool manifest dict; when present it is
                         injected as the TOOL_MANIFEST global so self-unpacking tools run from
                         the server's single source of truth (2026-09-11).

        Returns:
            The compiled tool function ready to be called.

        Raises:
            ToolCompilationError: If AST validation fails or compilation errors occur.
                This is a NON-RETRYABLE error (excluded from retry logic by class name).
        """
        # 1. Validate source via AST analysis (with tool-specific allowlist).
        errors = validate_source_code(source_code, tool_name)
        if errors:
            raise ToolCompilationError(
                f"Source validation failed for '{tool_name}': " + "; ".join(errors)
            )

        # 2a. FAIL-CLOSED: explicit source_hash shipped with tool_code_response (2026-08-20).
        hash_check = verify_tool_hash(tool_name, source_code, source_hash)
        if not hash_check.ok:
            raise ToolCompilationError(
                f"Integrity check failed for tool '{tool_name}' - delivered source does not match "
                f'the SERVER-provided hash. Refusing to execute unverified code.'
            )

        # 2b. Verify hash against allowlist (if configured) -- legacy advisory path.
        if not hash_in_allowlist(source_code, self._allowed_hashes):
            raise ToolCompilationError(
                f"Hash verification failed for tool '{tool_name}' - refusing to compile"
            )

        # 3. Ensure shared modules are available before compiling any tool.
        if not self._modules_installed:
            logger.warning("Loader: shared modules not installed, attempting auto-install")
            if self._shared_sources:
                self.install_shared_modules(self._shared_sources)

        # 4. Transform source code for exec() context (handle relative + absolute imports).
        transformed_source = transform_tool_source(source_code, tool_name)

        # 5-6. Build execution globals with proper module context + config constants.
        exec_globals = build_tool_exec_globals(tool_name)
        inject_config_constants(exec_globals, config_constants)
        if config_constants:
            logger.debug("Loader: injected %d config constants for tool '%s'",
                         len(config_constants), tool_name)

        # 6b. Self-unpacking tools (2026-09-11): inject the SERVER-shipped tool manifest so
        #     _active_manifest() inside the tool prefers it over its in-file fallback dict.
        if isinstance(tool_manifest, dict) and tool_manifest.get("runtime_name"):
            exec_globals["TOOL_MANIFEST"] = tool_manifest

        # 7. Install shared_deps BEFORE exec'ing the tool (their symbols are what its
        #    rewritten same-package assignments resolve against).
        _installed_count, failed_deps = install_shared_deps(
            tool_name, shared_deps, exec_globals, config_constants)

        # 7b. REQUIRED-DEP GATE (2026-09-20 fail-loud): the source_transform pipeline
        #     neutralizes 'from .module import X' lines to 'pass', so a dep that failed to
        #     install does NOT break compilation -- it breaks every CALL with NameError.
        #     If the tool actually imports one of the failed modules, fail HERE with an
        #     actionable message (and let the orchestrator's stale-cache self-heal refetch).
        if failed_deps:
            required = set(_same_package_dep_names(source_code)) & set((shared_deps or {}).keys())
            missing_required = [d for d in failed_deps if d.split(':', 1)[0] in required]
            if missing_required:
                raise ToolCompilationError(
                    f"Shared dependency install failed for tool '{tool_name}': "
                    + "; ".join(missing_required)
                    + " -- the tool source imports this module, so it cannot run without it. "
                      "(The delivered dep source raised during exec; since 2026-09-23 "
                       "_sandbox_bootstrap carries SANDBOX_BOOTSTRAP as an in-file literal and "
                       "does no disk reads at import -- a failure here means the SERVER shipped "
                       "a stale/broken copy of that module, so restart the SERVER from current code.)"
                )

        # 8. Execute the transformed source to define the tool function.
        try:
            exec(transformed_source, exec_globals)
        except Exception as e:
            logger.error("Loader: compilation failed for '%s': %s", tool_name, e)
            raise ToolCompilationError(
                f"Compilation failed for tool '{tool_name}': '{e}'"
            )

        # 9. Extract the main function (named after the tool).
        try:
            func = extract_tool_function(tool_name, exec_globals)
        except LookupError as e:
            raise ToolCompilationError(str(e)) from None

        # 10. Cache the compiled function.
        self._compiled_tools[tool_name] = func
        logger.debug("Loader: compiled and cached tool '%s'", tool_name)
        return func
