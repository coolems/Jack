"""Shared/framework module installation -- builds a real importable tree in sys.modules.

The SERVER ships shared sources ('utils', 'path_guard', 'ssrf_guard', 'ssrf_defense',
the 'web_interact' subpackage, 'app.provider_manager', 'app.dna.*') as plain text. This
turns that dict into LIVE modules registered in sys.modules -- nothing is written to disk:
  * non-'app.' keys live under the synthetic 'tools' namespace package
    ('utils' -> tools.utils, 'web_interact.shared_instance' -> tools.web_interact.shared_instance);
  * 'app.*' keys are registered under their LITERAL name so normal client imports resolve
    to SERVER-delivered code (from app.dna import get_agent).

Guarantees:
  * Idempotent -- modules already carrying '_remote_shared_module' are skipped.
  * Dependency order -- keys sorted by dot-depth, parents before children; same-depth keys
    keep the SERVER's send order, so 'ssrf_guard' arrives BEFORE the 'ssrf_defense' shim
    that imports it. Reordering get_shared_sources() breaks every network tool's SSRF
    protection; keep both places in sync.
  * Fail-closed integrity -- a promised SHA-256 prefix mismatch refuses exec.
  * Import-clean sources -- single-dot relative imports become absolute, and
    'from config import X' becomes an assignment from SERVER-shipped constants pre-exec.
  * DNA identity -- tools_response 'dna_data' is installed in MEMORY through the
    delivered app.dna package (set_dna_data); the CLIENT writes no DNA file.
"""

import importlib.machinery
import logging
import os
import re as _re
import sys
import types
from typing import Any, Dict, Optional

from .integrity import verify_shared_module_hash

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers (this file lives at CLIENT/tools/remote_tools/loader/shared_modules.py)
# ---------------------------------------------------------------------------

def _client_root() -> str:
    """Absolute path of the CLIENT root folder (four levels up from this file).

    This file lives at <CLIENT>/tools/remote_tools/loader/shared_modules.py, so the
    chain is loader -> remote_tools -> tools -> CLIENT. Going only three levels up
    would return <CLIENT>/tools.
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ---------------------------------------------------------------------------
# Small building blocks
# ---------------------------------------------------------------------------

def ensure_tools_namespace() -> types.ModuleType:
    """Create (once) the synthetic 'tools' namespace package in sys.modules.

    Without it Python cannot resolve 'from tools.XXX import YYY'. A real ModuleSpec is
    attached so importlib.util.find_spec('tools') inside compile_tool does not raise
    ValueError("tools.__spec__ is None") when building exec_globals.
    """
    existing = sys.modules.get('tools')
    if existing is not None:
        return existing

    tools_ns = types.ModuleType('tools')
    tools_ns.__path__ = []  # namespace package marker (empty list)
    tools_ns.__package__ = 'tools'
    tools_ns.__spec__ = importlib.machinery.ModuleSpec('tools', loader=None, is_package=True)
    sys.modules['tools'] = tools_ns
    logger.debug("Loader: created 'tools' namespace package in sys.modules")
    return tools_ns


def build_base_exec_globals() -> Dict[str, Any]:
    """Exec context shared by ALL delivered modules.

    Pre-injects the stdlib modules and typing names that shared code commonly uses at
    module level -- delivered sources are written for a normal interpreter where these
    are importable, so making them available directly keeps exec simple and uniform.
    """
    base: Dict[str, Any] = {'__builtins__': __builtins__}

    # Commonly needed stdlib modules (imported once; failures are non-fatal).
    for mod_name in ('os', 'sys', 'logging', 'json', 'base64', 're'):
        try:
            base[mod_name] = sys.modules.get(mod_name) or __import__(mod_name)
        except Exception:
            pass

    # typing attributes (Optional/Dict/List/...) for module-level annotations.
    try:
        import typing as _typing
        for attr in ('Optional', 'Dict', 'List', 'Any', 'Set', 'Tuple'):
            if hasattr(_typing, attr):
                base[attr] = getattr(_typing, attr)
    except Exception:
        pass

    return base


def sort_module_keys(keys) -> list[str]:
    """Sort module keys so parents install before children.

        Sort by dot-depth ONLY (stable), preserving the SERVER's send order among
        same-depth modules -- e.g. app.dna.name before app.dna.core/get_agent, which
        import their siblings at module level.
    """
    return sorted(keys, key=lambda k: k.count('.'))


def full_module_name(key: str) -> str:
    """Map a SERVER delivery key to the sys.modules name used on the CLIENT.

        Keys with an explicit top-level package ('app.dna.name') are registered under that
        EXACT name so normal client imports resolve to SERVER-delivered code; everything
        else goes under the 'tools' namespace.
    """
    return key if key.startswith('app.') else f'tools.{key}'


def ensure_ancestor_packages(full_mod_name: str) -> None:
    """Make sure every ancestor package of *full_mod_name* exists in sys.modules.

    Real client packages already importable from disk are left untouched; missing
    ancestors get lightweight namespace modules (empty __path__). A plain synthetic
    module is created ONLY when the real package cannot be imported -- creating one for
    'app.dna' would shadow the thin shell's lazy callables and break
    `from app.dna import get_agent`
    """
    parts = full_mod_name.split('.')
    client_root = _client_root()

    for depth in range(1, len(parts)):
        ancestor = '.'.join(parts[:depth])
        if ancestor in sys.modules:
            continue

        # Prefer the REAL client package on disk over a bare synthetic namespace module.
        real_pkg = None
        try:
            pkg_dir = os.path.join(client_root, *ancestor.split('.'))
            if os.path.isfile(os.path.join(pkg_dir, '__init__.py')):
                import importlib as _importlib
                real_pkg = _importlib.import_module(ancestor)
        except Exception as e:
            logger.debug("Loader: could not import real package '%s' (%s: %s) - using namespace module",
                         ancestor, type(e).__name__, e)

        if real_pkg is None or sys.modules.get(ancestor) is not real_pkg:
            anc_mod = types.ModuleType(ancestor)
            anc_mod.__path__ = []  # namespace package marker
            sys.modules[ancestor] = anc_mod


def build_module_exec_globals(full_mod_name: str, mod_key: str, base_globals: Dict[str, Any],
                              sorted_keys: list[str]) -> Dict[str, Any]:
    """Per-module exec context: identity + already-installed siblings.

    Sibling modules are injected under their SHORT name so delivered code can reference
    them directly; the real import path still works through sys.modules as well.
    """
    exec_globals = dict(base_globals)
    exec_globals['__name__'] = full_mod_name
    exec_globals['__file__'] = f'<remote_{mod_key}>'

    for sibling_key in sorted_keys:
        if sibling_key == mod_key:
            continue
        sibling_full = full_module_name(sibling_key)
        if sibling_full not in sys.modules:
            continue
        short_name = sibling_key.rsplit('.', 1)[-1] if '.' in sibling_key else sibling_key
        exec_globals[short_name] = sys.modules[sibling_full]

    return exec_globals


def attach_to_parent(mod: types.ModuleType, mod_key: str) -> None:
    """Register a freshly installed module on its parent package (or 'tools').

        Nested modules ('web_interact.shared_instance', 'app.dna.core') are attached to
        their literal parent so `import tools.web_interact` / `from app.dna import core`
        resolve; top-level modules go straight onto the 'tools' namespace. A REAL package's
        own non-module attribute is never clobbered by a delivered submodule.
    """
    if '.' in mod_key:
        # Parent name = literal ancestor for app.* keys, tools-prefixed otherwise.
        if mod_key.startswith('app.'):
            parent_name = '.'.join(mod_key.split('.')[:-1])
        else:
            parent_name = f"tools.{mod_key.rsplit('.', 1)[0]}"

        short_name = mod_key.rsplit('.', 1)[-1]
        parent_mod = sys.modules.get(parent_name)
        if parent_mod is None or not hasattr(parent_mod, '__dict__'):
            return

        existing_attr = parent_mod.__dict__.get(short_name)
        if (getattr(parent_mod, '__file__', None) and existing_attr is not None
                and not isinstance(existing_attr, types.ModuleType)):
            logger.debug("Loader: kept real package attribute '%s.%s' (not overwriting with delivered submodule)",
                         parent_name, short_name)
        else:
            setattr(parent_mod, short_name, mod)

        # Ensure the parent has __path__ so Python treats it as a package.
        if not hasattr(parent_mod, '__path__') or parent_mod.__path__ is None:
            parent_mod.__path__ = []

        logger.debug("Loader: attached '%s' to parent package '%s'", short_name, parent_name)
    else:
        tools_pkg = sys.modules.get('tools')
        if tools_pkg is not None and hasattr(tools_pkg, '__dict__'):
            setattr(tools_pkg, mod_key, mod)


# ---------------------------------------------------------------------------
# Source rewrites applied to DELIVERED shared modules (in-memory copy only -- the
# SERVER file on disk is never touched).
# ---------------------------------------------------------------------------

def _parse_name_pairs(names_str: str) -> list[tuple[str, str]]:
    """Parse 'A, B as C' into [('A','A'), ('B','C')]."""
    pairs = []
    for part in names_str.strip().split(','):
        part = part.strip()
        if not part:
            continue
        if ' as ' in part:
            original, alias = (x.strip() for x in part.split(' as ', 1))
        else:
            original = alias = part
        pairs.append((original, alias))
    return pairs


def rewrite_relative_imports(source_code: str, package_name: str) -> str:
    """Rewrite single-dot relative imports to absolute form for the real package.

    Delivered modules are exec'd with a synthetic namespace; 'from .x import y' would
    resolve against the wrong (or nonexistent) package path on disk. Rewriting to
    'from <package>.x import y' makes Python use sys.modules, where sibling delivered
    modules are already registered (e.g. app.dna.core -> from app.dna.name import get_name).
    """
    def _replace_module(match: _re.Match) -> str:
        ws, mod, names = match.groups()
        return f"{ws}from {package_name}.{mod} import {names}"

    rewritten = _re.sub(
        r'^(\s*)from\s+\.([a-zA-Z_][\w.]*)\s+import\s+(.+)$',
        _replace_module,
        source_code,
        flags=_re.MULTILINE,
    )

    def _replace_bare(match: _re.Match) -> str:
        ws, names = match.groups()
        return f"{ws}from {package_name} import {names}"

    return _re.sub(
        r'^(\s*)from\s+\.\s+import\s+(.+)$',
        _replace_bare,
        rewritten,
        flags=_re.MULTILINE,
    )


def rewrite_config_imports(source_code: str, config_constants: Dict[str, Any]) -> str:
    """Replace 'from config import X' lines with real assignments from SERVER constants.

    Delivered modules/tools may do module-level 'from config import NAME'.
    The CLIENT has no SERVER config module, so each such line becomes an assignment using
    the value shipped by the SERVER in tools_response 'config_constants'. Names the SERVER
    did not ship become a no-op 'pass' (keeps any surrounding try/except valid).

    No constants shipped at all -> source returned unchanged.
    """
    if not config_constants:
        return source_code

    def _replace(match: _re.Match) -> str:
        leading_ws, names_str = match.groups()
        lines_out = []
        for original, alias in _parse_name_pairs(names_str):
            value = config_constants.get(original)
            if value is not None:
                lines_out.append(f"{leading_ws}{alias} = {value!r}")
            else:
                logger.warning("Loader: config constant '%s' not shipped by SERVER - import neutralized", original)
                lines_out.append(f"{leading_ws}pass  # config constant '{original}' unavailable")
        return '\n'.join(lines_out) if lines_out else (leading_ws + 'pass')

    # Single-line form only: from config import A, B as C
    return _re.sub(
        r'^(\s*)from\s+config\s+import\s+(.+)$',
        _replace,
        source_code,
        flags=_re.MULTILINE,
    )

def inject_dna_data(dna_data: Optional[Dict[str, str]]) -> None:
    """Install SERVER-shipped DNA identity JSON in MEMORY (no disk write).

    The SERVER sends identity.json in tools_response 'dna_data'. It is handed to
    the delivered app.dna package through set_dna_data(), which makes the identity
    read from memory instead of from any file. The CLIENT therefore keeps no DNA
    folder and no DNA JSON on disk - nothing is written anywhere.

    A payload that does not parse as JSON is dropped with a warning (fail closed:
    an agent must never speak from garbage identity).
    """
    if not dna_data:
        return

    import json as _json

    payload = {}
    for fname, content in dna_data.items():
        try:
            _json.loads(content)
        except Exception as e:
            logger.warning("Loader: DNA payload '%s' is not valid JSON - dropped (%s)", fname, e)
            continue
        payload[fname] = content
    if not payload:
        return

    dna_pkg = sys.modules.get('app.dna')
    setter = getattr(dna_pkg, 'set_dna_data', None) if dna_pkg is not None else None
    if not callable(setter):
        logger.warning(
            "Loader: app.dna.set_dna_data unavailable - DNA identity stays "
            "unloaded (delivered app.dna.core missing?) "
            "[delivered keys: %s]", sorted(k for k in sys.modules if k.startswith('app.dna'))
        )
        return

    setter(payload)
    logger.info("Loader: DNA identity installed in memory (%d file(s): %s) - no client disk write",
                len(payload), ", ".join(payload))


# ---------------------------------------------------------------------------
# The installer (stateless -- all inputs are passed in; the loader facade owns state)
# ---------------------------------------------------------------------------

def install_shared_modules(sources: Dict[str, str],
                           expected_hashes: Optional[Dict[str, str]] = None,
                           config_constants: Optional[Dict[str, Any]] = None,
                           dna_data: Optional[Dict[str, str]] = None) -> bool:
    """Install shared utility modules for remote tool execution.
    Creates proper namespace package structure in sys.modules so that Python's import
    machinery can resolve 'from tools.utils import X' etc. Subpackage keys use dot
    notation ('web_interact.shared_instance'); parents are always installed first and
    nested modules are attached to their literal parent package (/).
    """
    try:
        # Step 1: 'tools' namespace package must exist for any tools.* resolution.
        ensure_tools_namespace()

        # Step 2: shared exec context (stdlib + typing) available to ALL modules.
        base_exec_globals = build_base_exec_globals()
        config_constants = dict(config_constants or {})

        # Step 3: install each module in dependency order (parents before children).
        sorted_keys = sort_module_keys(sources.keys())
        installed_count = 0

        for mod_key in sorted_keys:
            source_code = sources[mod_key]
            if not source_code:
                logger.debug("Loader: skipping empty shared module '%s'", mod_key)
                continue

            full_mod_name = full_module_name(mod_key)

            # Ancestors must exist before the module can be attached/imported.
            ensure_ancestor_packages(full_mod_name)

            # Skip if already installed (idempotent -- bootstrap + orchestrator both call this).
            existing = sys.modules.get(full_mod_name)
            if existing is not None and hasattr(existing, '_remote_shared_module'):
                logger.debug("Loader: shared module '%s' already installed", full_mod_name)
                continue

            # (2026-08-26 singleton fix): a REAL local copy of this module may have been
            # imported at boot BEFORE delivery -- e.g. app.provider_manager, whose class
            # already holds process state (the active provider + OCR models registered by
            # initialization_client.py). Replacing it with a fresh delivered instance would
            # silently wipe that state: delivered tool code doing
            # 'from app.provider_manager import ProviderManager' would then see an EMPTY
            # singleton and fail with "No active provider configured". The two copies are
            # byte-identical by design (see the docstring of app/provider_manager.py), so
            # keeping the pre-existing live instance is always safe: same code, real state.
            if existing is not None:
                logger.info(
                    "Loader: keeping pre-existing local module '%s' (boot-imported singleton with "
                    "live state -- delivered copy skipped because the code is byte-identical by design)",
                    full_mod_name,
                )
                continue

            mod = types.ModuleType(full_mod_name)
            mod.__file__ = f'<remote_{mod_key}>'
            mod.__name__ = full_mod_name

            # __package__: literal parent for app.* keys (their relative imports must
            # resolve against the real package), tools-prefixed otherwise. Top-level
            # 'tools.X' modules get 'tools'.
            if '.' in mod_key:
                mod.__package__ = full_mod_name.rsplit('.', 1)[0]
            else:
                mod.__package__ = 'tools'

            mod._remote_shared_module = True  # marker for the idempotency check above

            # FAIL-CLOSED INTEGRITY CHECK (2026-08-20): a promised hash that does not
            # match means tampering in transit or version skew -- refuse to run unverified
            # code instead of merely warning. Modules without a promised hash still install.
            check = verify_shared_module_hash(mod_key, source_code, (expected_hashes or {}).get(mod_key))
            if not check.ok:
                continue

            exec_globals = build_module_exec_globals(full_mod_name, mod_key, base_exec_globals, sorted_keys)

            # Make delivered sources import-clean BEFORE exec:
            #  a) single-dot relative imports -> absolute form against the REAL package;
            #  b) 'from config import X' lines -> real assignments from SERVER constants.
            source_code = rewrite_relative_imports(source_code, full_mod_name.rsplit('.', 1)[0])
            source_code = rewrite_config_imports(source_code, config_constants)

            # Execute the source to populate the module's namespace.
            exec(source_code, exec_globals)
            mod.__dict__.update(exec_globals)

            # Register in sys.modules AND attach to its parent package / tools namespace.
            sys.modules[full_mod_name] = mod
            attach_to_parent(mod, mod_key)

            installed_count += 1
            logger.info("Loader: shared module '%s' installed (%d chars)", full_mod_name, len(source_code))

        # DNA identity goes into memory AFTER all modules are installed (needs app.dna).
        inject_dna_data(dna_data)

        # NOTE: no working_root propagation here - tools resolve it per turn via the
        # tools.utils ContextVar published by agentic_mode() from the conversation's DB row.

        if installed_count > 0:
            logger.info("Loader: shared modules installation complete (%d new modules)", installed_count)
        else:
            logger.debug("Loader: no new shared modules to install")

        return True

    except Exception as e:
        import traceback
        logger.error("Loader: failed to install shared modules: %s", e)
        logger.debug(traceback.format_exc())
        return False
