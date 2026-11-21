"""Tool scanner package -- AST tool-definition extraction + source code serving.

One concern per module:
    log.py                       package logger
    constants.py                 EXCLUDED_TOOL_DIRS / SKIP_FILES
    paths.py                     _server_root() path helper
    node_to_python.py            _ast_node_to_python (AST -> Python object)
    dict_end_line.py             _find_dict_end_line (string-aware brace matching)
    extract_tool_ast.py          _extract_ollama_tool_ast (__tool_description__)
    build_def_from_signature.py  _build_def_from_signature (signature fallback)
    scan.py                      get_client_tools_dir / scan_tool_definitions
    shared_sources.py            _read_text / get_shared_utils_source / get_all_shared_sources
    framework_files.py           get_framework_sources (app.providers.* delivery)
    sha_prefix.py                _sha_prefix (integrity hashing primitive)
    shared_hashes.py             get_shared_hashes (hashes of all delivered modules)
    dna_data_files.py            get_dna_data_files (DNA identity JSON for delivery)
    same_package_imports.py      _find_same_package_imports ('from .module import X')
    tool_source.py               _locate_tool_file / get_tool_source_code(_with_deps)
    config_constants.py          extract_config_constants (config -> CLIENT constants)

Public names are re-exported here so existing imports keep working. Private helpers are
not: each sibling imports them from its own module and nothing outside the package reads
them - except _sha_prefix, needed by direct_ws/tool_code_handler.py for integrity hashes.
"""

from .catalog import build_tool_catalog
from .config_constants import extract_config_constants
from .constants import EXCLUDED_TOOL_DIRS, SKIP_FILES
from .dna_data_files import get_dna_data_files
from .framework_files import get_framework_sources
from .scan import get_client_tools_dir, scan_tool_definitions
from .sha_prefix import _sha_prefix  # noqa: F401 -- used by direct_ws/tool_code_handler.py
from .shared_hashes import get_shared_hashes
from .shared_sources import get_all_shared_sources, get_shared_utils_source
from .tool_source import get_tool_source_code, get_tool_source_code_with_deps

__all__ = [
    "EXCLUDED_TOOL_DIRS",
    "SKIP_FILES",
    "_sha_prefix",
    "build_tool_catalog",
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
