"""Fallback tool-definition builder from a function signature (AST only)."""

import ast
import json

def _build_def_from_signature(file_content, tool_name):

    """Fallback: build definition from function signature using AST."""

    try:

        tree = ast.parse(file_content)

    except SyntaxError:

        return None


    for node in ast.iter_child_nodes(tree):

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == tool_name:

            properties = {}

            required = []

            defaults_offset = len(node.args.args) - len(node.args.defaults)


            for idx, arg in enumerate(node.args.args):

                if arg.arg == 'self':

                    continue


                param_type = "string"

                if arg.annotation is not None:

                    ann_str = ast.unparse(arg.annotation)

                    if ann_str == 'int':

                        param_type = "integer"

                    elif ann_str == 'bool':

                        param_type = "boolean"

                    elif ann_str in ('float', 'int | float'):

                        param_type = "number"


                prop = {"type": param_type, "description": f"Parameter: {arg.arg}"}


                default_idx = idx - defaults_offset

                if default_idx >= 0 and default_idx < len(node.args.defaults):

                    try:

                        default_val = ast.literal_eval(node.args.defaults[default_idx])

                        json.dumps(default_val)

                        prop["default"] = default_val

                    except (ValueError, TypeError, json.JSONDecodeError):

                        pass

                else:

                    required.append(arg.arg)


                properties[arg.arg] = prop


            # Extract docstring for description

            description = f"Tool: {tool_name}"

            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, (ast.Constant,)):

                doc_val = node.body[0].value.value

                if isinstance(doc_val, str):

                    doc_first = doc_val.strip().split('\n')[0][:200]

                    description = doc_first


            return {

                "type": "function",

                "function": {

                    "name": tool_name,

                    "description": description,

                    "parameters": {

                        "type": "object",

                        "properties": properties,

                        "required": required if required else []

                    }

                }

            }


    return None
