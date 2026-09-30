"""loader -- split of the former single-file dynamic_loader.py (2026-08-21).

Public surface (imported by orchestrator/tools/__init__/logic modules):

    from tools.remote_tools.loader import DynamicModuleLoader, ToolCompilationError

Internal layout (one concern per module, for maintenance/debugging):

    errors           ToolCompilationError + subclasses   -- non-retryable compile failures
    policy           ALLOWED_TOOL_IMPORTS / _CALLS       -- auditable per-tool trust table
    ast_guard        SandboxedASTChecker                 -- AST security validation
    integrity        sha_prefix + fail-closed checks     -- SERVER hash verification
    source_transform transform_tool_source pipeline      -- import rewrites for exec()
    shared_modules   install_shared_modules + DNA sync   -- sys.modules installation
    globals_builder  exec-globals assembly               -- symbols/config/shared_deps
    loader           DynamicModuleLoader                 -- stateful public facade

The old file tools/remote_tools/dynamic_loader.py is now a thin compatibility shim
that re-exports from here, so every existing import path keeps working unchanged.
"""

from .errors import IntegrityError, LoaderError, SourceValidationError, ToolCompilationError
from .policy import ALLOWED_TOOL_CALLS, ALLOWED_TOOL_IMPORTS
from .ast_guard import SandboxedASTChecker, validate_source_code
from .integrity import (
    IntegrityCheckResult,
    hash_in_allowlist,
    sha_prefix,
    verify_shared_module_hash,
    verify_tool_hash,
)
from .source_transform import transform_tool_source
from .shared_modules import install_shared_modules as install_shared_modules_impl
from .globals_builder import build_tool_exec_globals, install_shared_deps
from .loader import DynamicModuleLoader

__all__ = [
    # Public facade + errors (the only names other modules import)
    "DynamicModuleLoader",
    "ToolCompilationError",
    "SourceValidationError",
    "IntegrityError",
    "LoaderError",
    # Policy / guard internals (exposed for tests & debugging)
    "ALLOWED_TOOL_CALLS",
    "ALLOWED_TOOL_IMPORTS",
    "SandboxedASTChecker",
    "validate_source_code",
    # Integrity scheme (pinned by tests/test_protocol_sync_20260820.py)
    "IntegrityCheckResult",
    "hash_in_allowlist",
    "sha_prefix",
    "verify_shared_module_hash",
    "verify_tool_hash",
    # Transformation + installation internals (exposed for unit testing)
    "transform_tool_source",
    "install_shared_modules_impl",
    "build_tool_exec_globals",
    "install_shared_deps",
]
