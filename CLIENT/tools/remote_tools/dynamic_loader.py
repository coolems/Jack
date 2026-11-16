"""COMPATIBILITY SHIM over the tools.remote_tools.loader package.

Modules behind it:
    loader/errors.py            ToolCompilationError + subclasses
    loader/policy.py            per-tool import/call allowlists (auditable trust table)
    loader/ast_guard.py         SandboxedASTChecker -- AST security validation
    loader/integrity.py         fail-closed SHA-256 prefix verification (both wire paths)
    loader/source_transform.py  the tool-source rewrite pipeline
    loader/shared_modules.py    sys.modules installation of shared code + DNA sync
    loader/globals_builder.py   exec-globals assembly, config constants, shared_deps
    loader/loader.py            DynamicModuleLoader -- stateful public facade

Kept so every existing import path resolves:
    from tools.remote_tools.dynamic_loader import DynamicModuleLoader, ToolCompilationError
New code imports from tools.remote_tools.loader.
"""

from .loader import DynamicModuleLoader, ToolCompilationError  # noqa: F401 -- re-exported public surface
from .loader.ast_guard import SandboxedASTChecker as _SandboxedASTChecker  # noqa: F401 (legacy private name)

__all__ = [
    "DynamicModuleLoader",
    "ToolCompilationError",
]
