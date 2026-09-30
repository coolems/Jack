"""Detection of same-package imports in a tool's source code."""

import ast

def _find_same_package_imports(file_content, tool_name):
    """Find same-package imports (from .module import X) in the source code.

    Returns a dict mapping module_name -> set of imported names.
    Example: {'shared_instance': {'get_web_interact', 'set_web_interact'}}

    Also detects bare imports from parent package (__init__.py):
      'from . import WebInteract as WI' → mapped to '__init__' key
    """
    try:
        tree = ast.parse(file_content)
    except SyntaxError:
        return {}

    imports = {}  # module_name -> set of names

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # Same-package import with explicit module (from .module import X)
            if node.level == 1 and node.module:
                mod_name = node.module
                for alias in node.names:
                    if mod_name not in imports:
                        imports[mod_name] = set()
                    imports[mod_name].add(alias.name)

            # Bare import from parent package (from . import Name as Alias)
            elif node.level == 1 and node.module is None:
                for alias in node.names:
                    if '__init__' not in imports:
                        imports['__init__'] = set()
                    # Store the original name (not the alias) so we can find it in __init__.py
                    imports['__init__'].add(alias.name)

    return {mod: sorted(names) for mod, names in imports.items()}
