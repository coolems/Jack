"""Source transformation -- rewrites delivered tool source for exec execution.

Delivered tools are written as if they lived in a real 'tools' package on disk
('from ..utils import X', 'from .shared_instance import Y', ...). On the CLIENT that
package exists only in sys.modules (built by shared_modules.py), so each import form is
rewritten into plain assignments against symbols injected into the exec globals at
compile time (see globals_builder.py). Steps run in this order; each owns only its forms:
  0. from x import (A, B)            -> from x import A, B   (paren list -> one line)
  1. from ..utils import X           -> X = utils.X        (multi-dot relative)
  2. from . import Name as Alias     -> Alias = Name       (bare parent package)
  3. from .module import (...)       -> pass               (same-package, multi-line)
  4. from .module import X           -> pass               (plain name: already injected)
     from .module import X as Y      -> Y = X              (an alias must stay bound)
  5. from tools.XXX(.YYY) import Z   -> Z = XXX_YYY.Z      (absolute internal, dotted OK)
  6. import tools.XXX                -> neutralized        (module is in exec globals)
  7. from utils.XXX import Y         -> Y = XXX.Y          (shared subpackage modules)
  8. from config import ...          -> pass               (constants injected pre-compile)
  9. def f(self, ...)                -> def f(...)         (legacy 'self' cleanup)

Every rewrite keeps the original line's indentation (anchored leading-whitespace
regexes), which is what keeps function-body imports valid after expansion.
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
            if ')' in _code_of(line):
                out_lines.append(start_indent + replacement)
                skip_until_close = False
            continue
        out_lines.append(line)

    return '\n'.join(out_lines)


# ---------------------------------------------------------------------------
# Step 1 -- multi-dot relative imports: 'from ..utils import X' -> 'X = utils.X'
# ---------------------------------------------------------------------------

#: Every 'from <module> import (' opener: relative ('..utils', '.mod'), absolute
#: ('tools.utils', 'utils.ssrf_defense'), bare parent ('.') or 'config'. The name list may
#: already start on the SAME line ('from tools.utils import (get_working_root,') or open on
#: its own line.
_PAREN_IMPORT_RE = _re.compile(r'^(\s*)from\s+(\S+)\s+import\s*\(\s*(.*)$')


def _strip_comment(text: str) -> str:
    """Drop a trailing '# ...' from one name-list fragment."""
    return _re.sub(r'\s*#.*$', '', text).strip().rstrip(',').strip()


def _code_of(text: str) -> str:
    """The chunk with its trailing '# ...' removed, keeping the rest as-is.

    Used to find the closing paren of an import list: a comment may itself contain ')'
    ('from tools.utils import (A,  # note (see docs)'), and scanning the RAW line would
    treat that comment paren as the end of the list - the flattener then stopped one line
    too early and left the remaining names as a stray indented line (SyntaxError)."""
    return _re.sub(r'\s*#.*$', '', text)


#: Stand-in for a triple-quoted string while the pipeline runs (see _mask_docstrings).
_DOC_MASK_FMT = "#__doc_mask_{n}__"
_DOC_STRING_RE = _re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')


def _mask_docstrings(source_code: str) -> tuple[str, dict]:
    """Replace every triple-quoted string with a sentinel token, return (masked, tokens).

    The rewrite steps are line-based, so a docstring that SHOWS an import form used to be
    rewritten too ('    from ..utils import (' became '    ( = utils.('), and a ')' inside
    documentation text could be mistaken for the closing paren of a real import. Masking
    keeps the pipeline away from strings; _unmask_docstrings puts them back untouched.
    """
    tokens: dict = {}

    def _repl(match: _re.Match) -> str:
        token = _DOC_MASK_FMT.format(n=len(tokens))
        tokens[token] = match.group(0)
        return token

    return _DOC_STRING_RE.sub(_repl, source_code), tokens


def _unmask_docstrings(source_code: str, tokens: dict) -> str:
    """Restore the masked strings exactly (a multi-line token re-expands to its lines)."""
    for token, original in tokens.items():
        source_code = source_code.replace(token, original)
    return source_code


def _flatten_paren_imports(source_code: str) -> str:
    """Collapse every parenthesized import statement onto one line.

    'from tools.utils import (get_working_root,\n resolve_path_to_dir)' becomes
    'from tools.utils import get_working_root, resolve_path_to_dir', and the same when the
    list opens on its own line. The line-based rewrites below can then treat it like any
    single-line import.

    Without this, an assignment step matched only the OPENING line and emitted
    '(get_working_root = utils.(get_working_root' - a SyntaxError that made every tool
    delivering this import form fail to compile on the CLIENT (read_file, write_file,
    edit_file, insert_lines, move_file, rename_file, delete_file, cua_screenshot) while
    working fine in the SERVER workspace, where it is a real import.

    Comments inside the name list are dropped (they carry no meaning for exec). Strings are
    masked by transform_tool_source, so documentation text is never scanned here. An
    unbalanced '(' is left untouched: that SyntaxError belongs to the tool, not the pipeline.
    """
    lines = source_code.split('\n')
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _PAREN_IMPORT_RE.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        indent, module, rest = m.group(1), m.group(2), m.group(3)
        parts: list[str] = []
        chunk, j, closed = rest, i, False
        while True:
            code = _code_of(chunk)      # a comment may contain ')' - scan the code only
            if ')' in code:
                parts.append(_strip_comment(code.split(')', 1)[0]))
                closed = True
                break
            parts.append(_strip_comment(code))
            j += 1
            if j >= len(lines):
                break
            chunk = lines[j]
        joined = ', '.join(p for p in parts if p)
        if closed and joined:
            out.append(f"{indent}from {module} import {joined}")
            i = j + 1
            continue
        out.append(line)
        i += 1
    return '\n'.join(out)


def _step_multi_dot_relative(source_code: str) -> str:
    """Rewrite 'from ..pkg.mod import A, B as C' into assignments against the module.

    Parenthesized forms are already flattened by transform_tool_source (see
    _flatten_paren_imports), so everything here is processed LINE BY LINE (not one global
    regex): when one import line expands into several assignment lines, indentation is
    always taken from THIS line. A single global regex cannot track position for
    multi-name imports inside function bodies.
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
    """Rewrite 'from . import Name as Alias' into a plain alias assignment.

    A bare parent-package import has no module name, so the target is the symbol already
    injected for the package's own facade; the alias is bound to it directly.
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
    """Rewrite 'from .XXX import YYY' for exec execution.

    The imported symbols are ALREADY in exec_globals (injected by globals_builder from the
    installed shared modules, merged by install_shared_deps), so a plain name needs no
    import statement and becomes an indented 'pass'. An ALIAS is different: neutralizing
    'from .XXX import YYY as ZZZ' to 'pass' leaves ZZZ unbound -> NameError on every call,
    so aliases become 'ZZZ = YYY'.
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
        modules under exactly that short name in exec_globals.
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

    The module object is injected into exec globals at compile time, so the import is
    replaced by a marker comment - there is nothing to bind on the CLIENT side.
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
    empty block behind (a bare comment would be a SyntaxError).
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
    masked, tokens = _mask_docstrings(source_code)
    transformed = masked
    transformed = _flatten_paren_imports(transformed)         # 0. 'import (\n A, B)' -> one line
    transformed = _step_multi_dot_relative(transformed)       # 1. from ..utils import X
    transformed = _step_bare_parent_import(transformed)       # 2. from . import Name as Alias
    transformed = _step_same_package_multiline(transformed)   # 3. from .mod import (...) -> pass
    transformed = _step_same_package_single(transformed)      # 4. from .mod import X -> X = X
    transformed = _step_absolute_tools_import(transformed)    # 5. from tools.X(.Y) import Z
    transformed = _step_import_tools(transformed)             # 6. import tools.X -> comment
    transformed = _step_utils_subpackage_import(transformed)  # 7. from utils.X import Y
    transformed = _strip_config_imports(transformed)          # 8. from config import ... -> pass
    transformed = _strip_legacy_self(transformed)             # 9. def f(self, ...) -> def f(...)
    transformed = _unmask_docstrings(transformed, tokens)     # 10. strings back, untouched

    logger.debug("Loader: source for '%s' transformed (%d -> %d chars)",
                 tool_name or '<unknown>', len(source_code), len(transformed))
    return transformed
