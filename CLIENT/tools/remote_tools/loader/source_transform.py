"""Source transformation -- rewrites delivered tool source for exec() execution.

Delivered tools are written as if they lived inside a real 'tools' package on disk
('from ..utils import X', 'from .shared_instance import Y', ...). On the CLIENT that
package only exists in sys.modules, built by shared_modules.py -- so every internal
import form is rewritten into plain assignments against symbols that are injected
into the exec globals at compile time (see globals_builder.py).

The pipeline is a sequence of small, independently testable steps. Order matters and
is documented per step; each step only touches the import forms it owns:

  1.  from ..utils import X            -> X = utils.X        (multi-dot relative)
  2.  from . import Name as Alias      -> Alias = Name       (bare parent-package)
  3.  from .module import (...)        -> pass               (same-package, multi-line)
  4.  from .module import X            -> pass               (same-package single-line plain name;
                                                              symbol already in injected globals -- a
                                                              self-assignment here would UnboundLocalError
                                                              inside function bodies, 2026-08-29 fix)
       from .module import X as Y      -> Y = X              (alias bound to the injected original
                                                              name -- 'pass' left aliases unbound and
                                                              crashed browser_screenshot, 2026-09-14 fix)
  5.  from tools.XXX(.YYY) import Z    -> Z = XXX_YYY.Z      (absolute internal, dotted OK)
  6.  import tools.XXX                 -> # neutralized       (module is in exec globals)
  7.  from utils.XXX import Y          -> Y = XXX.Y          (shared subpackage modules)
  8.  from config import ...           -> pass               (constants injected at compile time)
  9.  def f(self, ...)                 -> def f(...)         (legacy 'self' cleanup)

Every rewrite PRESERVES the original line's indentation (anchored leading-whitespace group regexes),
which is what keeps function-body imports valid after expansion into multiple lines.
"""

import logging
import re as _re

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers for import-name lists ("A, B as C")
# ---------------------------------------------------------------------------

def _parse_import_names(names_str: str) -> list[tuple[str, str]]:
    """Parse an import name list into (original_name, alias) pairs.

    'X'            -> ('X', 'X')
    'X as Y'       -> ('X', 'Y')
    'A, B as C'    -> [('A','A'), ('B','C')]
    """
    pairs: list[tuple[str, str]] = []
    for part in names_str.split(','):
        part = part.strip()
        if not part:
            continue
        if ' as ' in part:
            original, alias = (x.strip() for x in part.split(' as ', 1))
            pairs.append((original, alias))
        else:
            pairs.append((part, part))
    return pairs


def _emit_assignments(leading_ws: str, pairs: list[tuple[str, str]], value_expr) -> str:
    """Emit one assignment line per imported name, keeping the original indentation.

    *value_expr* is either a fixed string ('utils') or a callable (original_name) -> expr,
    allowing per-name expressions like 'utils.X'.
    """
    lines = []
    for original, alias in pairs:
        expr = value_expr(original) if callable(value_expr) else value_expr
        lines.append(f"{leading_ws}{alias} = {expr}")
    return '\n'.join(lines)


def _strip_multiline_paren_imports(source_code: str, pattern: str, replacement: str = 'pass') -> str:
    """Remove a multi-line parenthesized import whose first line matches *pattern*.

    The closing-paren line is replaced by '*replacement*' (default 'pass') so that an
    enclosing block (e.g. try/except) does not end up with an empty body -- which would
    be a SyntaxError ('expected an indented block').
    """
    start_re = _re.compile(pattern)
    out_lines: list[str] = []
    skip_until_close = False
    start_indent = ''

    for line in source_code.split('\n'):
        if not skip_until_close and start_re.match(line):
            skip_until_close = True
            start_indent = _re.match(r'^(\s*)', line).group(1)
            continue
        if skip_until_close:
            if ')' in line:
                out_lines.append(start_indent + replacement)
                skip_until_close = False
            continue
        out_lines.append(line)

    return '\n'.join(out_lines)


# ---------------------------------------------------------------------------
# Step 1 -- multi-dot relative imports: 'from ..utils import X' -> 'X = utils.X'
# ---------------------------------------------------------------------------

def _step_multi_dot_relative(source_code: str) -> str:
    """Rewrite 'from ..pkg.mod import A, B as C' into assignments against the module.

    Processed LINE BY LINE (not one global regex) so that when one import line expands
    into several assignment lines, indentation is always taken from THIS line -- this
    fixed position-tracking bugs with multi-name imports inside function bodies.
    """
    pattern = _re.compile(r'^(\s*)from\s+([.]{2,})([a-zA-Z_][\w.]*)\s+import\s+(.+)$')
    out_lines: list[str] = []

    for line in source_code.split('\n'):
        m = pattern.match(line)
        if not m:
            out_lines.append(line)
            continue
        leading_ws, _dots, module, names_str = m.groups()
        pairs = _parse_import_names(names_str)
        # 'from ..utils import get_working_root' -> 'get_working_root = utils.get_working_root'
        for original, alias in pairs:
            out_lines.append(f"{leading_ws}{alias} = {module}.{original}")

    return '\n'.join(out_lines)


# ---------------------------------------------------------------------------
# Step 2 -- bare parent-package imports: 'from . import Name as Alias' -> 'Alias = Name'
# ---------------------------------------------------------------------------

def _step_bare_parent_import(source_code: str) -> str:
        """Rewrite 'from . import Name as Alias' into 'Alias = Name'.

        The bare name is resolved from exec globals, where shared_modules.py has already
        injected the sibling package's public symbols. MUST run before step 4 -- its regex
        matches a dot followed by whitespace, which 'from .module import X' would not hit,
        but keeping the order stable avoids any overlap surprises.
        """
        pattern = _re.compile(r'^(\s*)from\s+\.\s+import\s+(.+)$')
        out_lines: list[str] = []

        for line in source_code.split('\n'):
            m = pattern.match(line)
            if not m:
                out_lines.append(line)
                continue
            leading_ws, names_str = m.groups()
            pairs = _parse_import_names(names_str)
            # Bare reference -- the symbol is injected into exec_globals by shared deps.
            for original, alias in pairs:
                out_lines.append(f"{leading_ws}{alias} = {original}")

        return '\n'.join(out_lines)


    # ---------------------------------------------------------------------------
# Step 3 -- same-package multi-line imports: 'from .module import ( ... )' -> pass
# ---------------------------------------------------------------------------

def _step_same_package_multiline(source_code: str) -> str:
    """Comment out multi-line parenthesized same-package imports.

    The symbols are already available in exec globals (shared_deps injection), so the
    whole statement is neutralized to an indented 'pass' -- which also keeps a
    try/except whose body was ONLY this import syntactically valid.
    """
    return _strip_multiline_paren_imports(
        source_code,
        r'^\s*from\s+\.\s*[a-zA-Z_][\w.]*\s+import\s*\(',
    )


# ---------------------------------------------------------------------------
# Step 4 -- same-package single-line imports: 'from .module import X' -> 'X = X'
# ---------------------------------------------------------------------------

def _step_same_package_single(source_code: str) -> str:
        """Rewrite 'from .XXX import YYY' for exec() execution.

        The imported symbols are ALREADY present in exec_globals (injected by
        globals_builder from the installed shared modules / merged by install_shared_deps),
        so plain names need no import statement at runtime -- they become an indented
        'pass'. An ALIAS ('from .XXX import YYY as ZZZ') is different: neutralizing it to
        'pass' leaves the alias name unbound -> NameError on every call. That exact bug hit
        browser_screenshot.py on 2026-09-14 ('name _ensure_page is not defined': the tool used
        'from .shared_instance import ensure_page as _ensure_page'). Aliases are now bound with
        an explicit assignment against the injected original name: 'ZZZ = YYY'. The RHS resolves
        through module globals (the injected symbol), so this is safe even inside function
        bodies -- the same pattern step 2 uses for bare parent-package imports.

        FIX (2026-08-29): plain-name self-assignments ('X = X') remain forbidden: that form is
        fatal when the import sits INSIDE A FUNCTION BODY (Python treats X as local for the whole
        function and reads the unbound local -> UnboundLocalError on every call). Plain names
        therefore stay neutralized to 'pass'; only ALIASES get an assignment, and their RHS
        references a DIFFERENT name that is never local.

        History: 2026-01-31 switched comments -> self-assignments; 2026-08-29 showed the
        plain-name assignment form unsound for function-body imports (neutralization);
        2026-09-14 added alias binding after the browser_screenshot NameError incident.
        """
        def _replace(match: _re.Match) -> str:
            leading_ws = match.group(1)
            pairs = _parse_import_names(match.group(3))
            out_lines = []
            for original, alias in pairs:
                if alias != original:
                    # Alias must be bound explicitly -- 'pass' would leave it unbound.
                    out_lines.append(f"{leading_ws}{alias} = {original}")
                else:
                    out_lines.append(
                        f"{leading_ws}pass  # same-package import neutralized (symbols injected into globals)")
            return '\n'.join(out_lines)

        return _re.sub(
            r'^(\s*)from\s+\.\s*([a-zA-Z_][\w.]*)\s+import\s+(.+)$',
            _replace,
            source_code,
            flags=_re.MULTILINE,
        )
# ---------------------------------------------------------------------------

def _step_absolute_tools_import(source_code: str) -> str:
    """Rewrite 'from tools.utils import X' / 'from tools.a.b import Y' to module refs.

    The dotted tail is flattened with underscores ('web_interact.shared_instance' ->
    'web_interact_shared_instance') because globals_builder.py registers subpackage
    modules under exactly that short name in exec_globals (2026-01-31 fix).
    """
    def _replace(match: _re.Match) -> str:
        leading_ws, module, names_str = match.groups()
        pairs = _parse_import_names(names_str.strip())
        # 'web_interact.shared_instance' -> 'web_interact_shared_instance' (injected name).
        short_module_name = module.replace('.', '_')
        return _emit_assignments(leading_ws, pairs, lambda original: f"{short_module_name}.{original}")

    return _re.sub(
        r'^(\s*)from\s+tools\.([a-zA-Z_][\w.]+)\s+import\s+(.+)$',
        _replace,
        source_code,
        flags=_re.MULTILINE,
    )


# ---------------------------------------------------------------------------
# Step 6 -- 'import tools.XXX' form (rare but possible) -> neutralized comment
# ---------------------------------------------------------------------------

def _step_import_tools(source_code: str) -> str:
    """Neutralize bare 'import tools.XXX' statements.

    The module object is injected into exec globals at compile time, so the import
    itself is replaced by a marker comment (nothing to bind on the CLIENT side).
    """
    def _replace(match: _re.Match) -> str:
        return f"# Absolute tools import (module injected): import tools.{match.group(2)}"

    return _re.sub(
        r'^(\s*)import\s+tools\.([a-zA-Z_]\w*)$',
        _replace,
        source_code,
        flags=_re.MULTILINE,
    )


# ---------------------------------------------------------------------------
# Step 7 -- shared subpackage modules: 'from utils.XXX import Y' -> 'Y = XXX.Y'
# ---------------------------------------------------------------------------

def _step_utils_subpackage_import(source_code: str) -> str:
    """Rewrite 'from utils.ssrf_defense import validate_url_not_ssrff' to module refs.

    Shared subpackage modules are registered in exec_globals under their short name,
    so the assignment resolves at runtime without any real import machinery.
    """
    def _replace(match: _re.Match) -> str:
        leading_ws, submod, names_str = match.groups()
        pairs = _parse_import_names(names_str.strip())
        return _emit_assignments(leading_ws, pairs, lambda original: f"{submod}.{original}")

    return _re.sub(
        r'^(\s*)from\s+utils\.([a-zA-Z_]\w*)\s+import\s+(.+)$',
        _replace,
        source_code,
        flags=_re.MULTILINE,
    )


# ---------------------------------------------------------------------------
# Step 8 -- SERVER config imports: 'from config import X' -> pass (constants injected)
# ---------------------------------------------------------------------------

def _strip_config_imports(source_code: str) -> str:
    """Remove every 'from config ...' line; the constants are injected at compile time.

    The CLIENT sandbox has no SERVER config module -- tools receive their values via
    config_constants in exec_globals (globals_builder.py). Multi-line parenthesized
    forms are handled first, then single lines; both become an indented 'pass' so a
    stripped import that was the ONLY statement of a try/except body cannot leave an
    empty block behind (2026-07-18 fix -- a bare comment would be a SyntaxError).
    """
    source_code = _strip_multiline_paren_imports(
        source_code,
        r'^\s*from\s+config\b.*?import\s*\(',
    )

    def _replace(match: _re.Match) -> str:
        leading_ws = match.group(1)
        return f"{leading_ws}pass  # Removed config import (constants available via config_constants)"

    return _re.sub(
        r'^(\s*)from\s+config\b.*?$',
        _replace,
        source_code,
        flags=_re.MULTILINE,
    )


# ---------------------------------------------------------------------------
# Step 9 -- legacy 'self' parameter cleanup on top-level function definitions
# ---------------------------------------------------------------------------

def _strip_legacy_self(source_code: str) -> str:
    """Strip a leading 'self' from (async) def signatures.

    Prevents "missing 1 required positional argument: 'self'" crashes when old tool
    code still carries method-style signatures in standalone functions.
    """
    return _re.sub(
        r'(async\s+def\s+\w+)\(\s*self\s*,\s*',
        r'\1(',
        source_code,
    )


# ---------------------------------------------------------------------------
# Public entry point -- the full pipeline
# ---------------------------------------------------------------------------

def transform_tool_source(source_code: str, tool_name: str = "") -> str:
    """Run *source_code* through every transformation step and return exec-ready code.

    Args:
        source_code: Original Python code string delivered by the SERVER.
        tool_name: Name of the tool (kept for logging/traceability; the rewrite rules
            themselves are tool-agnostic).

    Returns:
        Transformed source ready for exec() with injected globals.
    """
    transformed = source_code
    transformed = _step_multi_dot_relative(transformed)       # 1. from ..utils import X
    transformed = _step_bare_parent_import(transformed)       # 2. from . import Name as Alias
    transformed = _step_same_package_multiline(transformed)   # 3. from .mod import (...) -> pass
    transformed = _step_same_package_single(transformed)      # 4. from .mod import X -> X = X
    transformed = _step_absolute_tools_import(transformed)    # 5. from tools.X(.Y) import Z
    transformed = _step_import_tools(transformed)             # 6. import tools.X -> comment
    transformed = _step_utils_subpackage_import(transformed)  # 7. from utils.X import Y
    transformed = _strip_config_imports(transformed)          # 8. from config import ... -> pass
    transformed = _strip_legacy_self(transformed)             # 9. def f(self, ...) -> def f(...)

    logger.debug("Loader: source for '%s' transformed (%d -> %d chars)",
                 tool_name or '<unknown>', len(source_code), len(transformed))
    return transformed
