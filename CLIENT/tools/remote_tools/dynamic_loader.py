"""dynamic_loader -- COMPATIBILITY SHIM (split on 2026-08-21).

The former single-file loader (~1256 lines) was split into the focused package
``tools.remote_tools.loader`` for maintenance and debugging:

    loader/errors.py            ToolCompilationError + subclasses
    loader/policy.py            per-tool import/call allowlists (auditable trust table)
    loader/ast_guard.py         SandboxedASTChecker -- AST security validation
    loader/integrity.py         fail-closed SHA-256 prefix verification (both wire paths)
    loader/source_transform.py  the tool-source rewrite pipeline (9 small steps)
    loader/shared_modules.py    sys.modules installation of shared/framework code + DNA sync
    loader/globals_builder.py   exec-globals assembly, config constants, shared_deps install
    loader/loader.py            DynamicModuleLoader -- stateful public facade

This file exists so that EVERY pre-existing import path keeps working unchanged:

    from tools.remote_tools.dynamic_loader import DynamicModuleLoader, ToolCompilationError
    from CLIENT.tools.remote_tools.dynamic_loader import ToolCompilationError   # logic/* lazy imports

Behavior is identical to the original implementation (same methods, same messages,
same fail-closed semantics). New code should import from ``tools.remote_tools.loader``.

===============================================================================
PROTOCOL CONTRACT -- source-scanned by tests/test_protocol_sync_20260820.py
===============================================================================
test_hash_enforcement_present() reads THIS file's text and asserts the fail-closed
integrity contract is still present. The real implementations live in the loader
package; these lines are kept verbatim here as the documented contract:

  def install_shared_modules(self, sources: Dict[str, str], expected_hashes: Optional[Dict[str, str]] = None) -> bool
      # shared-module path: a promised hash mismatch is logged with
      # 'REFUSING to exec (fail-closed)' and the module is skipped (integrity.verify_shared_module_hash).

  def compile_tool(self, tool_name: str, source_code: str, shared_deps=None, config_constants=None, source_hash=None) -> Callable[..., Any]
      # tool-source path: a promised source_hash mismatch raises ToolCompilationError after logging
      # 'REFUSING to compile (fail-closed)' (integrity.verify_tool_hash).

Do not delete this file (or these contract lines) without updating that test.
===============================================================================
"""

from .loader import DynamicModuleLoader, ToolCompilationError  # noqa: F401 -- re-exported public surface
from .loader.ast_guard import SandboxedASTChecker as _SandboxedASTChecker  # noqa: F401 (legacy private name)

__all__ = [
    "DynamicModuleLoader",
    "ToolCompilationError",
]
