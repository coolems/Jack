"""AST node -> Python object conversion (no exec, no string round-trip)."""

import ast


def _unpack_literal(node, allowed):
    """Resolve the operand of a '*' / '**' unpack (PEP 448).

    Only a literal container is resolvable without executing the module, so a
    runtime expression stays a hard error instead of a misleading NoneType one.
    """
    value = _ast_node_to_python(node)
    if not isinstance(value, allowed):
        raise ValueError(
            f"Cannot convert AST node: '*'/'**' unpacks a non-literal "
            f"{type(node).__name__} whose value only exists at runtime"
        )
    return value


def _ast_node_to_python(node):
    """Convert an AST node directly to a Python object without string round-trip.

    Handles Constant, Dict, List, Tuple, Set, Name (for None/True/False),
    UnaryOp (negation), and Ellipsis. Falls back to ast.literal_eval on unparsed
    string for complex cases that still work with literal_eval.
    """
    if isinstance(node, ast.Constant):
        return node.value

    elif isinstance(node, ast.Dict):
        result = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:  # '**expr' slot (PEP 448)
                result.update(_unpack_literal(value, dict))
            else:
                result[_ast_node_to_python(key)] = _ast_node_to_python(value)
        return result

    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = []
        for elt in node.elts:
            if isinstance(elt, ast.Starred):  # '[*x]', '(*x)', '{*x}'
                items.extend(_unpack_literal(elt.value, (list, tuple, set)))
            else:
                items.append(_ast_node_to_python(elt))
        if isinstance(node, ast.Tuple):
            return tuple(items)
        if isinstance(node, ast.Set):
            return set(items)
        return items

    elif isinstance(node, ast.NameConstant):  # Python < 3.8 compatibility
        return node.value

    elif isinstance(node, ast.Num):  # Python < 3.8 compatibility
        return node.n

    elif isinstance(node, ast.Str):  # Python < 3.8 compatibility
        return node.s

    elif isinstance(node, ast.Name):
        if node.id == "None":
            return None
        elif node.id == "True":
            return True
        elif node.id == "False":
            return False

    elif isinstance(node, ast.Ellipsis):
        return Ellipsis

    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        val = _ast_node_to_python(node.operand)
        if val is not None:
            return -val

    # Fallback: try unparse + literal_eval for edge cases
    try:
        unparsed = ast.unparse(node)
        return ast.literal_eval(unparsed)
    except Exception:
        pass

    raise ValueError(f"Cannot convert AST node {type(node).__name__} to Python object")
