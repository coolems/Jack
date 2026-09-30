"""AST security guard -- validates delivered tool source BEFORE it is exec'd.

The visitor walks the parsed tree once and collects every violation into a list
of human-readable error strings (empty list == safe). It never raises for policy
violations; only an unparsable file produces a syntax-error entry, so callers get
a single complete report instead of failing on the first problem found.

Policy data lives in policy.py (per-tool allowlists); the hard defaults below are
deliberately conservative -- anything not explicitly allowed is blocked.
"""

import ast
import logging
from typing import Optional

from .policy import allowed_calls_for, allowed_imports_for

logger = logging.getLogger(__name__)


class SandboxedASTChecker(ast.NodeVisitor):
    """Single-pass AST visitor that detects dangerous patterns in source code.

    Usage:
        checker = SandboxedASTChecker(tool_name="python_exec")
        checker.visit(ast.parse(source))
        if checker.errors: ...  # reject the source
    """

    #: Modules that are dangerous by default; a tool may import them only when its
    #: name appears in policy.ALLOWED_TOOL_IMPORTS.
    DANGEROUS_MODULES = frozenset({
        'ctypes', 'socket', 'shutil', 'tempfile', 'glob',
        'signal', 'mmap', 'fcntl', 'select', 'selectors',
        'subprocess',  # subprocess is dangerous by default, allowed per-tool
    })

    #: Attribute names that expose dunder escape hatches (arbitrary code execution).
    DANGEROUS_ATTRS = frozenset({
        '__builtins__', '__subclasses__', '__globals__', '__code__',
        '__reduce__', '__reduce_ex__', '__class__',
    })

    #: Call targets on os/subprocess that execute commands or spawn processes.
    DANGEROUS_CALL_TARGETS = frozenset({
        'system', 'popen', 'Popen', 'call', 'run', 'check_output', 'check_call', 'startfile',
    })

    def __init__(self, tool_name: Optional[str] = None):
        """Create a validator. *tool_name* enables the per-tool allowlists from policy.py."""
        self.errors: list[str] = []
        self._tool_name = tool_name
        self._allowed_imports = allowed_imports_for(tool_name)
        self._allowed_calls = allowed_calls_for(tool_name)

    # -- imports -------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        """Reject imports of dangerous modules unless explicitly allowed for this tool.

        The check runs INSIDE the loop so EVERY import in a multi-name statement
        ('import os, subprocess') is validated (2026-08-21 fix -- previously only
        the last alias was checked and earlier dangerous imports slipped through).
        """
        for alias in node.names:
            module_name = alias.name.split('.')[0]
            if module_name not in self.DANGEROUS_MODULES:
                continue
            if module_name in self._allowed_imports:
                logger.debug(
                    "Loader: allowed import '%s' for tool '%s' (line %d)",
                    alias.name, self._tool_name, node.lineno,
                )
            else:
                self.errors.append(
                    f"Line {node.lineno}: Import of dangerous module '{alias.name}' blocked"
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Reject 'from <dangerous> import ...' unless explicitly allowed for this tool.

        Bare relative imports ('from . import X') have node.module is None -- there is
        nothing to check there (2026-08-21 fix -- previously that left module_name
        unbound and raised NameError during validation).
        """
        if not node.module:
            self.generic_visit(node)
            return
        module_name = node.module.split('.')[0]
        if module_name in self.DANGEROUS_MODULES:
            if module_name in self._allowed_imports:
                logger.debug(
                    "Loader: allowed import from '%s' for tool '%s' (line %d)",
                    node.module, self._tool_name, node.lineno,
                )
            else:
                self.errors.append(
                    f"Line {node.lineno}: Import from dangerous module '{node.module}' blocked"
                )
        self.generic_visit(node)

    # -- calls & attribute access --------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        """Block __import__() and dangerous call targets (os.system/subprocess/etc.)."""
        func = node.func
        if isinstance(func, ast.Name):
            if func.id == '__import__':
                self.errors.append(
                    f"Line {node.lineno}: Direct __import__() call blocked"
                )
        elif isinstance(func, ast.Attribute):
            # Dangerous process/command execution on os/subprocess.
            if (func.attr in self.DANGEROUS_CALL_TARGETS
                    and isinstance(func.value, ast.Name)
                    and func.value.id in ('os', 'subprocess')):
                call_name = f"{func.value.id}.{func.attr}"
                if call_name in self._allowed_calls:
                    logger.debug(
                        "Loader: allowed '%s()' for tool '%s' (line %d)",
                        call_name, self._tool_name, node.lineno,
                    )
                else:
                    self.errors.append(
                        f"Line {node.lineno}: Call to dangerous function '{call_name}()' blocked"
                    )
            # Dunder escape hatches used as a callable (e.g. obj.__class__()).
            elif func.attr in self.DANGEROUS_ATTRS:
                self.errors.append(
                    f"Line {node.lineno}: Access to dangerous attribute '{func.attr}' blocked"
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Block any attribute access to dunder escape-hatch names."""
        if node.attr in self.DANGEROUS_ATTRS:
            self.errors.append(
                f"Line {node.lineno}: Access to dangerous attribute '{node.attr}' blocked"
            )
        self.generic_visit(node)


def validate_source_code(source_code: str, tool_name: Optional[str] = None) -> list[str]:
    """Validate source code using AST analysis before execution.

    Args:
        source_code: Raw Python source to check.
        tool_name: Tool name; enables the per-tool allowlists from policy.py.

    Returns:
        List of error strings (empty if the source is safe). A SyntaxError is
        reported as a single entry, not raised.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        return [f"Syntax error at line {e.lineno}: {e.msg}"]

    checker = SandboxedASTChecker(tool_name=tool_name)
    checker.visit(tree)
    return checker.errors
