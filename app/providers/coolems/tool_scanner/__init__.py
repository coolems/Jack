"""Tool scanner package -- AST-based tool definition extraction + source code serving.

Split from the original single-file tool_scanner.py (839 lines) on 2026-09-07 into
small, focused modules for easier maintenance and debugging:

    log.py                     package logger (same name as before)
    constants.py               EXCLUDED_TOOL_DIRS / SKIP_FILES
    paths.py                   _server_root() path helper
    node_to_python.py          _ast_node_to_python (AST -> Python object)
    dict_end_line.py           _find_dict_end_line (string-aware brace matching)
    extract_tool_ast.py        _extract_ollama_tool_ast (__tool_description__ extraction)
    build_def_from_signature.py _build_def_from_signature (signature fallback def builder)
    scan.py                    get_client_tools_dir / scan_tool_definitions
    shared_sources.py          _read_text / get_shared_utils_source / get_all_shared_sources
    framework_files.py         get_framework_sources (app.providers.* delivery)
    sha_prefix.py              _sha_prefix (integrity hashing primitive)
    shared_hashes.py           get_shared_hashes (hashes for all delivered modules)
    dna_data_files.py          get_dna_data_files (DNA JSON state files)
    same_package_imports.py    _find_same_package_imports ('from .module import X' detection)
    tool_source.py             _locate_tool_file / get_tool_source_code / get_tool_source_code_with_deps
    config_constants.py        extract_config_constants (config/config.py -> CLIENT constants)

All public names are re-exported here, so existing imports keep working:
    from .tool_scanner import scan_tool_definitions  # etc.
"""

from .build_def_from_signature import _build_def_from_signature
from .config_constants import extract_config_constants
from .constants import EXCLUDED_TOOL_DIRS, SKIP_FILES
from .dict_end_line import _find_dict_end_line
from .dna_data_files import get_dna_data_files
from .extract_tool_ast import _extract_ollama_tool_ast
from .framework_files import get_framework_sources
from .log import logger
from .node_to_python import _ast_node_to_python
from .paths import _server_root
from .same_package_imports import _find_same_package_imports
from .scan import get_client_tools_dir, scan_tool_definitions
from .sha_prefix import _sha_prefix
from .shared_hashes import get_shared_hashes
from .shared_sources import _read_text, get_all_shared_sources, get_shared_utils_source
from .tool_source import _locate_tool_file, get_tool_source_code, get_tool_source_code_with_deps

__all__ = [
    "EXCLUDED_TOOL_DIRS",
    "SKIP_FILES",
    "extract_config_constants",
    "get_all_shared_sources",
    "get_client_tools_dir",
    "get_dna_data_files",
    "get_framework_sources",
    "get_shared_hashes",
    "get_shared_utils_source",
    "get_tool_source_code",
    "get_tool_source_code_with_deps",
    "scan_tool_definitions",
]
