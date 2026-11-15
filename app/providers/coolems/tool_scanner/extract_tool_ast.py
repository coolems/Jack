"""__tool_description__ extraction: AST first, string-aware text fallback second."""

import ast
import json

from .dict_end_line import _find_dict_end_line
from .node_to_python import _ast_node_to_python

def _extract_ollama_tool_ast(file_content):

    """Extract __tool_description__ dict from file content using AST - no imports executed."""

    # L7 fix: do NOT give up on a syntax error elsewhere in the file — fall through
    # to the string-aware text fallback below. Previously `return None` here made
    # the brace-fallback unreachable for exactly the files it was meant to rescue,
    # so any tool file with one bad line outside the dict was silently dropped.
    tree = None
    try:
        tree = ast.parse(file_content)
    except SyntaxError:
        pass

    if tree is not None:

        for node in ast.iter_child_nodes(tree):

            if isinstance(node, ast.Assign):

                for target in node.targets:

                    if isinstance(target, ast.Name) and target.id == '__tool_description__':

                        try:

                            return _ast_node_to_python(node.value)

                        except (ValueError, TypeError, KeyError):

                            pass


    # Fallback for edge cases where the AST path above could not convert the value.
    # L7 fix (two parts):
    #   1. Brace matching is now string-aware via _find_dict_end_line() — the old
    #      naive counter broke on '{' / '}' inside string literals and truncated
    #      or mis-framed the extracted snippet.
    #   2. The extracted snippet is parsed with ast.literal_eval BEFORE json.loads,
    #      so Python-style dicts (single quotes, None/True/False) are no longer
    #      silently dropped when they are not strict JSON.

    file_lines = file_content.split('\n')

    s_line = None
    for i_ln, ln in enumerate(file_lines):
        if '__tool_description__' in ln and '=' in ln:
            s_line = i_ln
            break

    if s_line is not None:
        e_line = _find_dict_end_line(file_lines, s_line)

        if e_line is not None:
            snippet = '\n'.join(file_lines[s_line:e_line])
            eq_pos = snippet.index('=') + 1
            dict_str = snippet[eq_pos:].strip()

            try:
                return ast.literal_eval(dict_str)
            except (ValueError, SyntaxError):
                pass

            try:
                return json.loads(dict_str)
            except json.JSONDecodeError:
                pass

    return None
