"""Exec-globals builder -- assembles the global namespace a delivered tool runs in.

A compiled tool does not run "bare": it needs (a) proper module identity so relative
imports and __spec__ behave, (b) every public symbol of the installed shared modules
injected for BARE access ('from ..utils import X' was rewritten to 'X = utils.X', and
same-package imports were rewritten to bare-name assignments), (c) subpackage modules
registered under their short underscored name ('web_interact.shared_instance' ->
'web_interact_shared_instance'), and (d) the SERVER-shipped config constants.

Shared deps (sibling tool sources shipped in 'tool_code_response.shared_deps') get
their own exec context built from the same ingredients, are executed, registered as
real modules under tools.<dep>, and their public symbols flow into the tool's globals --
that is what makes 'from .shared_instance import get_web_interact' work at runtime.

FAIL-LOUD CONTRACT (2026-09-20 fix for the python_exec SANDBOX_BOOTSTRAP NameError):
install_shared_deps() used to swallow every per-dep failure as a WARNING and return,
so a tool whose rewritten source referenced an injected symbol compiled fine but died
at RUNTIME with 'NameError: name X is not defined' (the import line had been neutralized
to 'pass' by source_transform). Now install_shared_deps() returns the list of failed dep
names, compile_tool() scans the delivered source for same-package imports (AST) and RAISES
ToolCompilationError when a required shared_dep failed to install -- the tool fails at
compile time with an actionable message instead of half-wired at runtime.
"""

import ast
import logging
import sys
import types
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Shared modules whose module object + public symbols are ALWAYS injected by short name.
# (2026-09): ssrf_guard is the new single source of truth for SSRF defense; the
# legacy ssrf_defense shim re-exports it and must stay installed too.
CORE_SHARED_MODULES = ('utils', 'path_guard', 'ssrf_guard', 'ssrf_defense', 'tool_bootstrap')

# Stdlib pre-injected into shared-dep exec contexts (deps commonly use these at top level).
_DEP_STDLIB = ('os', 'sys', 'logging', 'json', 'base64', 're', 'datetime', 'pytz')


def _iter_remote_shared_modules():
    """Yield (full_name, module) for every installed SERVER-delivered tools.* module."""
    for full_name, mod in list(sys.modules.items()):
        if not full_name.startswith('tools.') or not hasattr(mod, '_remote_shared_module'):
            continue
        yield full_name, mod


def inject_remote_shared_symbols(exec_globals: Dict[str, Any]) -> None:
    """Inject every public symbol of all installed shared modules into *exec_globals*.

    Three layers (kept identical to the original loader's behavior):

      1. Core modules ('utils', 'path_guard', 'ssrf_guard', 'ssrf_defense'): the module object under
         its short name AND each public attribute for bare access.
      2. Subpackage modules: every public symbol of every other tools.* delivered module
         (e.g. WebInteract from web_interact) -- needed by rewritten bare-name imports.
      3. Subpackage module objects registered under their SHORT UNDERSCORED name so the
         transformed 'web_interact_shared_instance.get_web_interact' style refs resolve.

    Later modules win on symbol collisions (dict update order), same as before.
    """
    # Layer 1 -- core shared modules: module object + individual public attributes.
    for short_name in CORE_SHARED_MODULES:
        shared_mod = sys.modules.get(f'tools.{short_name}')
        if shared_mod is None:
            continue
        exec_globals[short_name] = shared_mod
        for attr in dir(shared_mod):
            if not attr.startswith('_'):
                exec_globals[attr] = getattr(shared_mod, attr)

    # Layer 2 -- public symbols of every other delivered module (bare-name resolution).
    for full_name, mod in _iter_remote_shared_modules():
        short = full_name[len('tools.'):]
        if short in CORE_SHARED_MODULES:
            continue
        for attr in dir(mod):
            if not attr.startswith('_'):
                exec_globals[attr] = getattr(mod, attr)

    # Layer 3 -- subpackage modules under their short underscored name.
    for full_name, mod in _iter_remote_shared_modules():
        short = full_name[len('tools.'):]
        exec_globals[short.replace('.', '_')] = mod


def inject_config_constants(exec_globals: Dict[str, Any], config_constants: Optional[Dict[str, Any]]) -> None:
    """Inject SERVER-shipped config constants (replaces stripped 'from config import' lines)."""
    if not config_constants:
        return
    for const_name, const_value in config_constants.items():
        exec_globals[const_name] = const_value


def build_tool_exec_globals(tool_name: str) -> Dict[str, Any]:
    """Build the base exec globals for one tool module (identity + shared symbols).

    The returned dict is ready to receive config constants and shared-dep symbols;
    compile_tool() then exec's the transformed source in it.
    """
    import importlib.util

    module_path = f"tools.{tool_name}"
    package_path = "tools"

    exec_globals: Dict[str, Any] = {
        '__name__': module_path,
        '__package__': package_path,
        '__file__': f'<remote_tool_{tool_name}>',
        # A real spec keeps __spec__-aware code working; find_spec('tools') is safe because
        # shared_modules.ensure_tools_namespace() attached a proper ModuleSpec (2026-07-18).
        '__spec__': importlib.util.find_spec('tools') or None,
        '__builtins__': __builtins__,
    }

    inject_remote_shared_symbols(exec_globals)

    # Fallback: if shared utils failed to install, try the CLIENT-side common module so at
    # least get_working_root() and friends resolve (2026-01 behavior preserved).
    if exec_globals.get('utils') is None:
        try:
            from app.utils import common as _client_common
            exec_globals['utils'] = _client_common
            for attr in dir(_client_common):
                if not attr.startswith('_'):
                    exec_globals[attr] = getattr(_client_common, attr)
            logger.info("Loader: fell back to CLIENT app.utils.common for shared utils")
        except Exception as e:
            logger.warning("Loader: CLIENT common fallback also failed: %s", e)

    return exec_globals


def build_dep_exec_globals(dep_name: str, config_constants: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the exec context for one shared dep (sibling tool source).

    Deps get module identity under tools.<dep>, pre-injected stdlib modules, ALL already
    installed shared-module symbols (module objects AND individual public attributes so
    bare names work inside deps -- 2026-01-31 fix), and the config constants.
    """
    dep_globals: Dict[str, Any] = {
        '__name__': f'tools.{dep_name}',
        '__package__': 'tools',
        '__builtins__': __builtins__,
    }

    for mod in _DEP_STDLIB:
        if mod in sys.modules:
            dep_globals[mod] = sys.modules[mod]

    # ALL shared modules into the dep's namespace (module objects + public attributes).
    for full_name, mod_obj in _iter_remote_shared_modules():
        short = full_name[len('tools.'):]
        dep_globals[short.rsplit('.', 1)[-1]] = mod_obj
        for attr in dir(mod_obj):
            if not attr.startswith('_'):
                dep_globals[attr] = getattr(mod_obj, attr)

    inject_config_constants(dep_globals, config_constants)
    return dep_globals


def _same_package_dep_names(source_code: str) -> List[str]:
    """Return the same-package module names a tool source imports (AST-based).

    Mirrors the SERVER's tool_scanner._find_same_package_imports(): every level-1
    'from .module import ...' statement. Used by compile_tool() to decide which shared_deps
    are REQUIRED: if one of these failed to install, the rewritten source references a
    symbol that does not exist in exec_globals -> guaranteed runtime NameError (2026-09-20).

    'from . import Name' (level 1, no module) is NOT counted -- those symbols come from
    already-installed shared modules, not from the tool's own shared_deps dict.
    Returns [] when the source cannot be parsed (compile_tool raises on that upstream).
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return []
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            if node.module not in names:
                names.append(node.module)
    return names


def install_shared_deps(tool_name: str, shared_deps: Optional[Dict[str, str]],
                        exec_globals: Dict[str, Any],
                        config_constants: Optional[Dict[str, Any]] = None):
    """Compile same-package dependencies and merge their symbols into the tool's globals.

    Each dep source is transformed with the SAME pipeline as the tool itself (so its own
    internal imports are rewritten identically), executed in a dedicated context, then
    registered as a real module under tools.<dep> in sys.modules -- which also makes it
    visible to any LATER-compiled sibling that imports it.

    Must run BEFORE the tool source is exec'd: the symbols are what its rewritten
    same-package assignments resolve against (2026-01 fix).

    Args:
        tool_name: Tool being compiled (for logging only -- deps register under their own names).
        shared_deps: Optional {dep_name: source_code} from 'tool_code_response.shared_deps'.
        exec_globals: The tool's globals dict; every public dep symbol is merged into it.
        config_constants: SERVER constants, injected into the deps' contexts too.

    Returns:
        (installed, failed) -- installed: number of deps successfully installed;
        failed: list of 'dep_name: <error>' strings for deps that could not be exec'd.
        A dep failure is NOT fatal by itself (the tool may genuinely not use it), but the
        CALLER must check compile_tool()'s required-dep gate -- see _same_package_dep_names().
    """
    from .source_transform import transform_tool_source

    if not shared_deps:
        return 0, []

    installed = 0
    failed: List[str] = []
    for dep_name, dep_source in shared_deps.items():
        try:
            transformed_dep = transform_tool_source(dep_source, dep_name)
            dep_globals = build_dep_exec_globals(dep_name, config_constants)

            exec(transformed_dep, dep_globals)

            # Register as a real module so later siblings can import it normally.
            dep_full_name = f'tools.{dep_name}'
            dep_mod = types.ModuleType(dep_full_name)
            dep_mod.__dict__.update(dep_globals)
            sys.modules[dep_full_name] = dep_mod

            # Merge ALL public symbols into the tool's globals -- this is what makes
            # 'from .shared_instance import get_web_interact' work at runtime.
            for attr in list(dep_globals.keys()):
                if not attr.startswith('_'):
                    exec_globals[attr] = dep_globals[attr]

            installed += 1
            logger.debug("Loader: installed shared_dep '%s' (%d symbols injected)",
                         dep_name, sum(1 for a in dep_globals if not a.startswith('_')))
        except Exception as e:
            # FAIL-LOUD (2026-09-20): log at ERROR level and report the failure to the
            # caller. The old WARNING-only swallow is what let python_exec compile with a
            # missing SANDBOX_BOOTSTRAP global and die deep inside a call instead.
            logger.error("Loader: failed to install shared_dep '%s' for tool '%s': %s",
                         dep_name, tool_name, e)
            failed.append("%s: %s" % (dep_name, e))

    return installed, failed


def extract_tool_function(tool_name: str, exec_globals: Dict[str, Any]) -> Callable[..., Any]:
    """Pull the compiled tool callable out of its exec globals.

    Contract (unchanged from the original loader): the module must define a top-level
    function whose name is EXACTLY *tool_name*. Anything else is a compilation failure --
    the caller raises ToolCompilationError with this message verbatim.
    """
    func = exec_globals.get(tool_name)
    if func is None or not callable(func):
        logger.error("Loader: no callable '%s' found in compiled module", tool_name)
        raise LookupError(
            f"No callable function '{tool_name}' found after compiling source for tool '{tool_name}'"
        )
    return func
