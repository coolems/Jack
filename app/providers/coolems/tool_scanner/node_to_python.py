"""AST node -> Python object conversion (no exec, no string round-trip)."""

import ast

def _ast_node_to_python(node):

    """Convert an AST node directly to a Python object without string round-trip.


    Handles Constant, Dict, List, Tuple, Set, Name (for None/True/False),

    UnaryOp (negation), and Ellipsis. Falls back to ast.literal_eval on unparsed

    string for complex cases that still work with literal_eval.

    """

    if isinstance(node, ast.Constant):

        return node.value

    elif isinstance(node, ast.Dict):

        keys = [_ast_node_to_python(k) for k in node.keys]

        values = [_ast_node_to_python(v) for v in node.values]

        return dict(zip(keys, values))

    elif isinstance(node, (ast.List, ast.Tuple)):

        result = [_ast_node_to_python(elt) for elt in node.elts]

        return tuple(result) if isinstance(node, ast.Tuple) else result

    elif isinstance(node, ast.Set):

        return {_ast_node_to_python(elt) for elt in node.elts}

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
