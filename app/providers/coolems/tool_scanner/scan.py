"""tools/ directory scanning: find every tool definition without executing code."""

import os

from .build_def_from_signature import _build_def_from_signature
from .constants import EXCLUDED_TOOL_DIRS, SKIP_FILES
from .extract_tool_ast import _extract_ollama_tool_ast
from .log import logger
from .paths import _server_root

def get_client_tools_dir():

    """Find the tools/ directory relative to SERVER root."""

    server_root = _server_root()

    tools_dir = server_root / "tools"

    if tools_dir.is_dir():

        return str(tools_dir)


    logger.warning("[SERVER] Could not find tools directory for tool serving at %s", tools_dir)

    return None

def scan_tool_definitions(tools_dir):

    """Scan tools directory and extract all tool definitions using AST (no exec). Returns (definitions_list, allowed_tools_names)."""

    definitions = []

    allowed_tools = []


    try:

        for item in os.listdir(tools_dir):

            item_path = os.path.join(tools_dir, item)

            if not os.path.isdir(item_path) or item in EXCLUDED_TOOL_DIRS:

                continue


            for file in os.listdir(item_path):

                if not file.endswith('.py') or file == '__init__.py':

                    continue


                tool_name = file.replace('.py', '')

                if tool_name in SKIP_FILES:

                    continue


                full_path = os.path.join(item_path, file)


                with open(full_path, 'r', encoding='utf-8') as f:

                    file_content = f.read()


                tool_def = None


                # Try AST-based extraction of __tool_description__ (no imports executed)

                if '__tool_description__' in file_content:

                    tool_def = _extract_ollama_tool_ast(file_content)


                # Fallback: build definition from function signature via AST

                if not tool_def and (f'def {tool_name}(' in file_content or f'async def {tool_name}(' in file_content):

                    tool_def = _build_def_from_signature(file_content, tool_name)


                if tool_def:

                    definitions.append(tool_def)

                    allowed_tools.append(tool_name)


    except Exception as e:

        logger.error(f"[SERVER] Error scanning tools directory: {e}", exc_info=True)


    return definitions, allowed_tools
