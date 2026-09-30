"""AST security guard for python_exec -- the PRIMARY code check.

WHAT THIS FILE DOES
===================
Walks the parsed AST of user-supplied Python code and reports violations in two layers:

  LAYER 1 - PROFILE-DRIVEN IMPORT CHECKS (soft, profile-controlled)
      The active per-profile blocked-libs set (shipped by the SERVER -- see
      _get_blocked_libs() in python_exec.py) decides which modules are import-blocked.
      An EMPTY set means "allow everything" BY DESIGN (admin clean set); for that
      case python_exec() skips the guardrails entirely before this file ever runs.

  LAYER 2 - HARD FLOOR (always active for every NON-EMPTY profile set)
      The AST/regex guardrail over arbitrary Python is fundamentally whack-a-mole:
      any single missed pattern is a full escape, because executed code runs in a
      normal interpreter with all builtins available. The hard floor therefore
      blocks the generic PRIMITIVES of escape instead of enumerating payloads:

        * attribute calls to open/exec/eval/compile/__import__ on ANY object
          (closes `b = globals()['__builtins__']; f = b.open; f(...)`, which was a
          verified live escape before the 2026-07-14 refactor)
        * subscript lookups through globals()/vars() results or any tainted name
        * getattr with a dynamic (non-literal) attribute name
        * calls/assignments through names that were assigned from a dangerous expression

      TAINT MODEL: expressions whose value is "dangerous" are marked. A Name bound
      to a dangerous expression becomes tainted; using a tainted name in an
      assignment, call, subscript or getattr is blocked. Taint sources (see
      _taint_reason): globals()/vars() calls, subscripts through them, the bare
      __builtins__ reference (in script context it resolves to the builtins MODULE),
      getattr with dynamic names, and attribute access holding a hard-floor builtin
      or dangerous dunder on any object.

      The model is computed in a PRE-PASS over the whole tree before visiting, so
      detection is independent of statement order (an assignment earlier in the file
      taints uses later in the file; no reliance on visitor traversal order). It is
      deliberately a conservative over-approximation -- shadowing edge cases may be
      over-blocked, never under-blocked (fail-closed).

DELIVERY NOTE (CLIENT sandbox)
==============================
This module is delivered to CLIENTs as a same-package dependency of python_exec.py:
tool_scanner._find_same_package_imports() picks it up from the tool's
'from ._ast_guard import ...' line, and dynamic_loader transforms that into
bare-name assignments with the symbols injected at compile time. It therefore
imports ONLY stdlib 'ast'. All policy data is passed in as a namespace object by
python_exec (see run_ast_check) so this file has ZERO coupling to sibling modules --
which keeps it trivially unit-testable and immune to loader transform edge cases.

DEBUGGING
=========
Every violation string names the pattern that matched, e.g.:
    "b.open() -- attribute call to hard-floor builtin 'open' on any object is blocked"
The exploit battery (escape probes) and legitimate-code controls (regression probes)
live in tests/test_sandbox_bootstrap.py.
"""

import ast
import os
import re


# ============================================================================
# AST helpers (public names: the CLIENT loader injects public symbols only)
# ============================================================================

def get_attribute_chain(node):
    """Build full attribute chain string (e.g., 'os.path.join')."""
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return '.'.join(reversed(parts))


def resolve_subscript_root(node):
    """Walk through chained subscripts and resolve to the root name or call func name.

    Handles patterns like:
        globals()['os']()           -> 'globals'
        vars()['__builtins__']['eval']()  -> 'vars'
        __builtins__['system']()    -> '__builtins__'
    """
    current = node
    while isinstance(current, ast.Subscript):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    if isinstance(current, ast.Call) and isinstance(current.func, ast.Name):
        return current.func.id
    return None


def _is_forbidden_name_node(node, policy):
    """Check if a node is a Name referencing a forbidden function. Returns the name or None."""
    if isinstance(node, ast.Name) and node.id in policy.FORBIDDEN_FUNCTIONS:
        return node.id
    return None


def _check_lambda_for_forbidden_calls(lambda_node, violations, policy):
    """Recursively check inside a Lambda's body for forbidden function calls.

    Scans the lambda expression body (and any nested lambdas/calls) to detect
    attempts like: map(lambda x: eval(x), data) or sorted(data, key=lambda y: exec(y))
    """
    for child in ast.walk(lambda_node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name) and child.func.id in policy.FORBIDDEN_FUNCTIONS:
                violations.append(
                    f"lambda(... {child.func.id} ...) -- calling forbidden function "
                    f"'{child.func.id}' inside lambda is blocked"
                )
            elif isinstance(child.func, ast.Attribute):
                full_path = get_attribute_chain(child.func)
                for mod, func in policy.FORBIDDEN_ATTR_CALLS:
                    if full_path.endswith(f"{mod}.{func}"):
                        violations.append(
                            f"lambda(... {full_path}) -- calling dangerous "
                            f"'{mod}.{func}' inside lambda is blocked"
                        )
                        break


# ============================================================================
# The visitor
# ============================================================================

class ASTSecurityVisitor(ast.NodeVisitor):
    """Walk the AST and detect dangerous patterns.

    Args:
        forbidden_modules: the ACTIVE profile-driven import blocklist (frozenset).
            An empty set means "allow everything" -- all import checks are skipped
            (admin clean set; python_exec() normally skips the whole guardrail in
            that case, this keeps the visitor usable standalone).
        policy: namespace with the security constants from _policy.py
            (FORBIDDEN_FUNCTIONS, HARD_FLOOR_ATTR_CALL_NAMES, DANGEROUS_ATTR_ACCESS,
            TINTED_CALL_NAMES, HIGHER_ORDER_FUNCTIONS, FORBIDDEN_ATTR_CALLS, ...).

    Usage:
        visitor = ASTSecurityVisitor(blocklist, policy)
        visitor.prepare(tree)   # taint pre-pass (MUST run before visit)
        visitor.visit(tree)     # detection pass; results in visitor.violations
    """

    def __init__(self, forbidden_modules, policy):
        self.violations = []
        self.imported_modules = set()
        self._forbidden_aliases = set()      # names assigned from FORBIDDEN_FUNCTIONS
        self._dangerous_from_imports = set()  # names imported via 'from os import <unsafe>'
        self.forbidden_modules = frozenset(forbidden_modules) if forbidden_modules is not None else frozenset()
        self.P = policy

        # --- taint bookkeeping (hard floor), filled by prepare() ---
        self._tainted_names = set()   # Name ids whose bound value is dangerous
        self._tainted_nodes = {}      # id(node) -> reason, for subexpressions

    # ------------------------------------------------------------------
    # Taint model (hard floor) -- order-independent pre-pass helpers
    # ------------------------------------------------------------------

    def _taint_reason(self, node):
        """Return a human-readable reason if *node*'s value is dangerous, else None.

        This is the heart of the 2026-07-14 fix: it answers "does this expression
        hand over something that can escape working_root or execute code?" without
        depending on where in the file the expression appears.
        """
        P = self.P

        if isinstance(node, ast.Name):
            # Bare __builtins__ reference: in script context (`python -c`) it IS the
            # builtins module -- direct access to open/exec/eval without any import.
            if node.id == "__builtins__":
                return "bare __builtins__ reference (the builtins module in script context)"
            if node.id in self._tainted_names:
                return f"name '{node.id}' derived from a dangerous expression"
            return None

        if isinstance(node, ast.Attribute):
            reason = self._taint_reason(node.value)
            if reason:
                return f"attribute on {reason}"
            # Attribute holding a hard-floor builtin or dangerous dunder on ANY object:
            # b.open / x.__dict__ -- the value is callable/inspectable escape material.
            if node.attr in P.HARD_FLOOR_ATTR_CALL_NAMES or node.attr in P.DANGEROUS_ATTR_ACCESS:
                return f"hard-floor attribute '{node.attr}'"
            return None

        if isinstance(node, ast.Subscript):
            reason = self._taint_reason(node.value)
            if reason:
                return f"subscript through {reason}"
            return None

        if isinstance(node, ast.Call):
            # globals()/vars() produce namespace dicts whose '__builtins__' entry is
            # the builtins MODULE in script context -- subscripting them is a dynamic
            # object/function lookup (the verified escape root).
            if isinstance(node.func, ast.Name) and node.func.id in P.TINTED_CALL_NAMES:
                return f"{node.func.id}() namespace"
            # getattr with a non-literal attribute name yields an unknown object.
            if isinstance(node.func, ast.Name) and node.func.id == "getattr":
                if len(node.args) >= 2:
                    arg = node.args[1]
                    if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                        return "getattr with dynamic attribute name"
                if len(node.args) >= 1:
                    reason = self._taint_reason(node.args[0])
                    if reason:
                        return f"getattr through {reason}"
            # Call whose receiver is tainted (b.open(...), tainted['x'](), ...).
            func = node.func
            if isinstance(func, ast.Attribute):
                base_reason = self._taint_reason(func.value)
                attr_floor = func.attr in P.HARD_FLOOR_ATTR_CALL_NAMES or func.attr in P.DANGEROUS_ATTR_ACCESS
                if base_reason:
                    return f"call through {base_reason}"
                if attr_floor:
                    return f"call of hard-floor attribute '{func.attr}'"
            elif isinstance(func, ast.Subscript):
                reason = self._taint_reason(func.value)
                if reason:
                    return f"call through subscript on {reason}"
            elif isinstance(func, ast.Call):
                inner_reason = self._taint_reason(func.func) if isinstance(func.func, (ast.Name, ast.Attribute)) else None
                if inner_reason:
                    return f"chained call through {inner_reason}"
            return None

        return None

    def prepare(self, tree):
        """Taint pre-pass over the whole tree. MUST be called before visit().

        Pass 1a marks tainted NAMES (targets of assignments whose value is
        dangerous). Pass 1b marks tainted NODES (every subexpression that yields a
        dangerous value), which lets visit_Call/visit_Subscript detect chained use
        regardless of statement order.
        """
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = list(getattr(node, "targets", [])) or (
                    [node.target] if isinstance(node, ast.AnnAssign) else []
                )
                value = getattr(node, "value", None)
                if value is not None and self._taint_reason(value):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            self._tainted_names.add(target.id)
                        elif isinstance(target, (ast.Tuple, ast.List)):
                            for elt in target.elts:
                                if isinstance(elt, ast.Name):
                                    self._tainted_names.add(elt.id)

        for node in ast.walk(tree):
            reason = self._taint_reason(node)
            if reason:
                self._tainted_nodes[id(node)] = reason

    def _is_tainted_expr(self, node):
        """True if this expression's value is known-dangerous (hard floor)."""
        if isinstance(node, ast.Name):
            return node.id == "__builtins__" or node.id in self._tainted_names
        return id(node) in self._tainted_nodes

    # ------------------------------------------------------------------
    # Import checks (LAYER 1 -- profile-driven)
    # ------------------------------------------------------------------

    def visit_Import(self, node):
        for alias in node.names:
            module_name = alias.name.split('.')[0]
            # Active profile-driven blocklist (empty set = allow everything).
            if module_name in self.forbidden_modules or alias.name in self.forbidden_modules:
                self.violations.append(
                    f"import {alias.name} -- cannot import dangerous module '{alias.name}'"
                )
            # FIX (2026-08-19): block BARE imports of dangerous modules too.
            # Safe names remain reachable via "from os.path import join" etc.
            elif module_name in self.P.DANGEROUS_MODULES_FROM_IMPORT or alias.name in self.P.DANGEROUS_MODULES_FROM_IMPORT:
                self.violations.append(
                    f"import {alias.name} -- cannot import dangerous module "
                    f"'{alias.name}' (use 'from os.path import join' style for safe names)"
                )
            self.imported_modules.add(module_name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            top_module = node.module.split('.')[0]
            full_module = node.module
            # Modules in DANGEROUS_MODULES_FROM_IMPORT ("os"/"importlib") are NOT checked
            # here: their from-imports are governed by the per-name SAFE_OS_IMPORTS logic.
            _per_name_mod = top_module in self.P.DANGEROUS_MODULES_FROM_IMPORT or full_module in self.P.DANGEROUS_MODULES_FROM_IMPORT
            if not _per_name_mod and (top_module in self.forbidden_modules or full_module in self.forbidden_modules):
                self.violations.append(
                    f"from {node.module} import ... -- cannot import from dangerous "
                    f"module '{node.module}'"
                )
            if top_module in self.P.DANGEROUS_MODULES_FROM_IMPORT or full_module in self.P.DANGEROUS_MODULES_FROM_IMPORT:
                for alias in node.names:
                    # Allow safe os imports (pure path math / constants only).
                    if top_module == "os" and alias.name in self.P.SAFE_OS_IMPORTS:
                        continue
                    self._dangerous_from_imports.add(alias.name)
                    self.violations.append(
                        f"from {node.module} import {alias.name} -- cannot import "
                        f"'{alias.name}' from dangerous module '{node.module}'"
                    )
            self.imported_modules.add(top_module)
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Assignment checks -- aliasing of forbidden functions + hard-floor taint
    # ------------------------------------------------------------------

    def _check_assign_value(self, node):
        """Shared logic for Assign / AnnAssign right-hand sides."""
        targets = list(getattr(node, "targets", [])) or (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        value = getattr(node, "value", None)
        if value is None:
            return

        # 1. Direct alias of a forbidden builtin: x = eval
        if isinstance(value, ast.Name) and value.id in self.P.FORBIDDEN_FUNCTIONS:
            for target in targets:
                if isinstance(target, ast.Name):
                    self._forbidden_aliases.add(target.id)
                    self.violations.append(
                        f"alias {target.id} = {value.id} -- cannot alias "
                        f"forbidden function '{value.id}'"
                    )
        # 2. Tuple unpack containing a forbidden builtin: x, y = eval, open
        elif isinstance(value, ast.Tuple):
            for elt in value.elts:
                if isinstance(elt, ast.Name) and elt.id in self.P.FORBIDDEN_FUNCTIONS:
                    self.violations.append(
                        f"tuple unpack with {elt.id} -- cannot alias forbidden "
                        f"function '{elt.id}'"
                    )

        # 3. HARD FLOOR (2026-07-14): binding escape material is blocked at the
        #    assignment, not only at call time: b = globals()['__builtins__'],
        #    f = b.open, e = x.exec -- each line is flagged with its taint reason.
        reason = self._taint_reason(value)
        if reason:
            for target in targets:
                name = target.id if isinstance(target, ast.Name) else "(tuple)"
                self.violations.append(
                    f"assignment {name} = <dangerous expression> -- blocked "
                    f"(reason: {reason})"
                )

    def visit_Assign(self, node):
        self._check_assign_value(node)
        self.generic_visit(node)

    # FIX (2026-07-14): annotated assignments were never checked before --
    # `f: object = b.open` slipped through the alias/taint logic entirely.
    def visit_AnnAssign(self, node):
        if node.value is not None:
            self._check_assign_value(node)
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Subscript checks -- dynamic namespace lookups (hard floor)
    # ------------------------------------------------------------------

    def visit_Subscript(self, node):
        # FIX (2026-07-14): the old code only blocked a subscript when it was used
        # DIRECTLY as a call (globals()[...]()). Attribute access on the result --
        # b = globals()['__builtins__']; f = b.open -- passed everything. Now any
        # subscript through a tainted base is a dynamic lookup and is flagged here
        # (covers bare expression statements; assignments are also flagged in
        # _check_assign_value, calls in visit_Call).
        if self._is_tainted_expr(node.value):
            root = resolve_subscript_root(node) or "tainted expression"
            self.violations.append(
                f"dynamic function/object lookup via {root} is blocked "
                f"(subscript through globals()/vars() namespace)"
            )
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Attribute access checks (dangerous dunders at ACCESS time)
    # ------------------------------------------------------------------

    def visit_Attribute(self, node):
        # FIX (2026-08-19): block DANGEROUS_ATTR_ACCESS at ACCESS time too, not only
        # when the attribute is called. Live escape test: os.__dict__['system'] pulled
        # the shell function off a module via plain subscript access.
        if node.attr in self.P.DANGEROUS_ATTR_ACCESS:
            self.violations.append(
                f"attribute access to '{node.attr}' is blocked (dangerous dunder)"
            )
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Call checks -- the bulk of security detection
    # ------------------------------------------------------------------

    def visit_Call(self, node):
        P = self.P

        # 1. Direct function call (e.g., eval(...), exec(...))
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in P.FORBIDDEN_FUNCTIONS:
                self.violations.append(
                    f"{func_name}() -- cannot use '{func_name}' directly"
                )
            elif func_name in self._forbidden_aliases:
                self.violations.append(
                    f"{func_name}() -- call through alias of forbidden function is blocked"
                )
            # HARD FLOOR (2026-07-14): calls through a tainted name.
            elif func_name in self._tainted_names:
                self.violations.append(
                    f"{func_name}() -- call through dynamic namespace lookup "
                    f"(globals()/vars()/__builtins__ derived) is blocked"
                )
            elif func_name in self._dangerous_from_imports:
                self.violations.append(
                    f"{func_name}() -- calling '{func_name}' (imported from dangerous "
                    f"module) is blocked"
                )
            # Block dir(__builtins__), dir(__class__), etc.
            elif func_name == "dir":
                if node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Name) and arg.id in P.DANGEROUS_ATTR_ACCESS:
                        self.violations.append(
                            f"dir({arg.id}) -- enumeration of dangerous builtins is blocked"
                        )

        # 2. Attribute call (e.g., subprocess.run(...), os.system(...))
        elif isinstance(node.func, ast.Attribute):
            full_path = get_attribute_chain(node.func)

            for mod, func in P.FORBIDDEN_ATTR_CALLS:
                if full_path.endswith(f"{mod}.{func}"):
                    self.violations.append(
                        f"{full_path}() -- cannot use {mod}.{func}"
                    )
                    break

            # HARD FLOOR (2026-07-14): the verified escape. In `python -c` context,
            # globals()['__builtins__'] IS the builtins module, so b.open / b.exec are
            # plain attribute calls with zero forbidden tokens in source. Block these
            # names on ANY base object, plus any call whose receiver is tainted.
            if node.func.attr in P.HARD_FLOOR_ATTR_CALL_NAMES:
                self.violations.append(
                    f"{full_path}() -- attribute call to hard-floor builtin "
                    f"'{node.func.attr}' on any object is blocked"
                )
            elif self._is_tainted_expr(node.func.value):
                self.violations.append(
                    f"{full_path}() -- call through dynamic namespace lookup "
                    f"(globals()/vars()/__builtins__ derived) is blocked"
                )

            # getattr with dangerous function names
            if full_path == "getattr" and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    target_func = arg.value
                    if target_func in P.FORBIDDEN_FUNCTIONS or target_func in P.HARD_FLOOR_ATTR_CALL_NAMES:
                        self.violations.append(
                            f"getattr(..., '{target_func}') -- cannot dynamically "
                            f"access forbidden function '{target_func}'"
                        )
                elif isinstance(arg, ast.Name):
                    self.violations.append(
                        f"getattr(..., {arg.id}) -- dynamic attribute access is "
                        f"dangerous and blocked"
                    )
                # FIX (2026-08-19): string-concatenation defeats the literal check.
                elif isinstance(arg, ast.BinOp) and isinstance(
                    arg.op, (ast.Add, ast.Mod, ast.FloorDiv)
                ):
                    self.violations.append(
                        "getattr(..., <concatenated string>) -- dynamic attribute name "
                        "built from string concatenation is blocked"
                    )

            # Check for __builtins__ or other dangerous attributes
            attr_name = node.func.attr if hasattr(node.func, 'attr') else ""
            if attr_name in P.DANGEROUS_ATTR_ACCESS:
                self.violations.append(
                    f"{full_path}() -- access to '{attr_name}' is blocked"
                )

        # 3. Chained call detection: func_call(...) (...)
        elif isinstance(node.func, ast.Call):
            inner = node.func
            if isinstance(inner.func, ast.Name) and inner.func.id == "getattr":
                if len(inner.args) >= 2:
                    arg = inner.args[1]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value in P.FORBIDDEN_FUNCTIONS or arg.value in P.HARD_FLOOR_ATTR_CALL_NAMES:
                            self.violations.append(
                                f"getattr(..., '{arg.value}')() -- chained call to "
                                f"forbidden function via getattr is blocked"
                            )
                    elif isinstance(arg, ast.Name):
                        self.violations.append(
                            f"getattr(...)() -- chained dynamic attribute access is blocked"
                        )
                # Check first arg of getattr (the object being accessed)
                if len(inner.args) >= 1:
                    if isinstance(inner.args[0], ast.Name) and inner.args[0].id in P.FORBIDDEN_FUNCTIONS:
                        self.violations.append(
                            f"getattr({inner.args[0].id}, ...)() -- getattr on "
                            f"forbidden function is blocked"
                        )
                    elif self._is_tainted_expr(inner.args[0]):
                        # HARD FLOOR (2026-07-14): getattr(tainted, 'anything')()
                        self.violations.append(
                            "getattr(<tainted>, ...)() -- call through dynamic namespace "
                            "lookup is blocked"
                        )
            elif isinstance(inner.func, ast.Name) and inner.func.id == "__import__":
                self.violations.append(
                    f"__import__(...)() -- chained import call is blocked"
                )
            # HARD FLOOR (2026-07-14): any other chained call through a tainted
            # receiver, e.g. getattr(b, 'op' + 'en')('x') or b.open(...)('...').
            if self._is_tainted_expr(inner):
                self.violations.append(
                    "chained call through dynamic namespace lookup (tainted expression) is blocked"
                )

        # 4. Indirect execution via subscript (handles chained subscripts properly).
        # Kept for the direct-call form globals()[...](); visit_Subscript also flags
        # the plain lookup, so this branch mainly adds a clear message at call time.
        if isinstance(node.func, ast.Subscript):
            root_name = resolve_subscript_root(node.func)
            if root_name in ("vars", "globals", "__builtins__"):
                self.violations.append(
                    f"dynamic function lookup via {root_name} is blocked"
                )
            elif self._is_tainted_expr(node.func):
                self.violations.append(
                    "call through dynamic namespace lookup (tainted subscript) is blocked"
                )

        # 5. Forbidden functions passed to higher-order functions
        if isinstance(node.func, ast.Name) and node.func.id in P.HIGHER_ORDER_FUNCTIONS:
            if node.args:
                first_arg = node.args[0]
                forbidden_in_arg = _is_forbidden_name_node(first_arg, P)
                if forbidden_in_arg:
                    self.violations.append(
                        f"{node.func.id}({forbidden_in_arg}, ...) -- passing "
                        f"forbidden function '{forbidden_in_arg}' to higher-order "
                        f"function is blocked"
                    )
            for kw in node.keywords:
                if kw.arg and kw.arg in ("key", "func"):
                    forbidden_in_kw = _is_forbidden_name_node(kw.value, P)
                    if forbidden_in_kw:
                        self.violations.append(
                            f"{node.func.id}(..., {kw.arg}={forbidden_in_kw}) -- "
                            f"passing forbidden function '{forbidden_in_kw}' as "
                            f"keyword arg to higher-order function is blocked"
                        )

        # 6. Lambda-style indirect execution in arguments (ALL args + kwargs)
        all_args = list(node.args) + [kw.value for kw in node.keywords]
        for arg_node in all_args:
            if isinstance(arg_node, ast.Lambda):
                _check_lambda_for_forbidden_calls(arg_node, self.violations, P)

        self.generic_visit(node)


# ============================================================================
# PATH SCOPE CHECK (2026-08-23) -- ALWAYS-ON hard floor, independent of the
# per-profile blocked-libs set.
#
# WHY THIS EXISTS
#   The profile-driven layers above govern IMPORTS and dangerous calls. An admin
#   clean set ("modules": []) disables ALL of them BY DESIGN -- which also means
#   open(r"<DATA_DRIVE>\models\...") or os.listdir("<SYSTEM_DIR>") executes with zero friction  # examples
#   even though the tool contract says "stay inside working_root". This check
#   closes that gap at PATH-LITERAL level: every string constant in the submitted
#   code is inspected BEFORE execution, and any literal that resolves OUTSIDE the
#   configured working_root blocks the whole run.
#
# WHAT COUNTS AS A PATH CANDIDATE (string constants only -- no disk access here)
#   * Windows drive/absolute:  X:\data\x , X:/system , bare "X:"   # any drive letter
#   * UNC / backslash-rooted:  \\server\\share , \Windows\System32
#   * POSIX absolute, strict charset [A-Za-z0-9._-], >=2 segments: /etc/passwd
#     (strict charset keeps regexes like "^/a/" or "/\d+/" from false-matching)
#   * ANY string with ".." as a FULL path segment: ..\\x , ../secrets
#     (the child process cwd IS working_root, so ".." climbs out of the tree)
#   Non-candidates by design: URLs ("https://..."), relative paths inside the
#   root ("<project>/x.txt", ".gitignore"), plain prose.
#
# LIMITATIONS (documented -- same residual class as every static guardrail)
#   * Paths built dynamically at runtime (chr() math, env-var expansion, f-string
#     FormattedValue parts) are not visible to a pre-exec scan. Non-admin profiles
#     still block "os" entirely via their blocked-libs set; the hard-floor taint
#     model above covers the builtins-module route.
#   * A literal that is INSIDE working_root but resolved elsewhere at runtime
#     (e.g. symlink targets) cannot be seen statically.
#
# CONTRACT: run_path_scope_check(code, working_root) -> (has_violations, [msgs])
# Pure stdlib (ast/os/re), zero coupling to sibling modules -- delivery-safe for
# the CLIENT sandbox exactly like the rest of this file.
# ============================================================================

_WIN_DRIVE_ABS_RE = re.compile(r'^[A-Za-z]:([/\\].*)?$')                 # e.g. X:\data , X:/x , "X:"  (any drive letter)
_POSIX_ABS_STRICT_RE = re.compile(r'^/[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]*)+$')  # /etc/passwd (>=2 segments)
_DOTDOT_SEGMENT_RE = re.compile(r'(^|[/\\])\.\.([/\\]|$)')           # ".." as full segment


def _is_path_candidate(s):
    """True if a string constant looks like an absolute path or a traversal."""
    if not isinstance(s, str) or "://" in s:          # URLs are never filesystem paths here
        return False
    if _WIN_DRIVE_ABS_RE.match(s):
        return True
    if re.match(r'^\\\\[A-Za-z0-9._\-]+\\', s):  # UNC \\server\share (needs server name + second backslash)
        return True
    if _POSIX_ABS_STRICT_RE.match(s):
        return True
    if _DOTDOT_SEGMENT_RE.search(s):
        return True
    return False


def run_path_scope_check(code, working_root):
    """Pre-execution guardrail: block code that references paths OUTSIDE working_root.

    Scans every string constant in the AST (f-string literal parts included -- they
    are plain Constants; dynamic FormattedValue parts are skipped by design). A
    candidate path is resolved with os.path.abspath/normcase and compared to
    *working_root* case-insensitively on Windows. Any hit outside the root, or any
    ".." full-segment traversal string, produces a violation.

    Args:
        code: user-supplied Python source (pre-wrap -- line numbers stay accurate).
        working_root: absolute path of the allowed tree.

    Returns:
        (has_violations, violations) -- same contract as run_ast_check().
    """
    if not isinstance(code, str) or not code.strip():
        return False, []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Unparseable code cannot execute in the child either (it dies before any
        # I/O), so there is no path to scope here.
        return False, []

    if not working_root or not isinstance(working_root, str):
        return True, ["path-scope guardrail has no configured working_root -- refusing to run (fail-closed)"]

    root_norm = os.path.normcase(os.path.abspath(working_root))
    violations = []
    seen = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        s = node.value
        reason = None
        if _DOTDOT_SEGMENT_RE.search(s):
            reason = "'..' path traversal -- the child process cwd is working_root, so '..' climbs out of it"
        elif _is_path_candidate(s):
            resolved = os.path.normcase(os.path.abspath(s))
            if resolved != root_norm and not resolved.startswith(root_norm + os.sep):
                reason = f"path resolves to {resolved} which is OUTSIDE working_root ({working_root})"

        if reason:
            snippet = s if len(s) <= 60 else s[:57] + "..."
            msg = (f"line {node.lineno}: path literal {snippet!r} -- {reason}")
            if msg not in seen:
                seen.add(msg)
                violations.append(msg)

    return len(violations) > 0, violations


# ============================================================================
# Entry point used by python_exec.py
# ============================================================================

def run_ast_check(code, forbidden_modules, policy):
    """Parse code into AST and detect dangerous patterns (PRIMARY check).

    Args:
        code: the user-supplied Python source.
        forbidden_modules: active profile-driven import blocklist (frozenset;
            empty = allow everything for import checks -- hard floor still runs).
        policy: namespace with _policy.py constants (see module docstring).

    Returns:
        (has_violations, violations_list) -- same contract as the legacy
        _check_forbidden_ast().
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False, ["Syntax error in code -- falling back to pattern scan"]

    visitor = ASTSecurityVisitor(forbidden_modules, policy)
    visitor.prepare(tree)   # taint pre-pass (order-independent hard floor)
    visitor.visit(tree)     # detection pass
    violations = visitor.violations
    return len(violations) > 0, violations
