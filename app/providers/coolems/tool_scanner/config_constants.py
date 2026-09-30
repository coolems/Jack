"""Config constants extraction for CLIENT-side tools (AST, no exec)."""

import ast
import json

from .log import logger
from .node_to_python import _ast_node_to_python
from .paths import _server_root

def extract_config_constants():
    """Extract config constants needed by CLIENT-side tools.

    Uses AST to safely parse config/config.py and extract only the constants
    that tools actually need (WEB_SEARCH_*, FILE_MAX_*, etc.).

    This avoids sending the entire config module (which contains paths, keys, etc.)
    while still providing the values tools reference via 'from config import ...'.

    Returns:
        Dict mapping constant name -> value. Empty dict if extraction fails.
    """
    # The set of constants that CLIENT-side tools actually need
    NEEDED_CONSTANTS = {
        # Web search
        'WEB_SEARCH_DEFAULT_MAX_RESULTS',
        'WEB_SEARCH_DDGS_DEFAULT_RESULTS',
        'WEB_SEARCH_WIKIPEDIA_TIMEOUT',
        'WEB_SEARCH_DDGS_TIMEOUT',
        'WEB_SEARCH_BING_TIMEOUT',
        'WEB_SEARCH_SEARXNG_TIMEOUT',
        'WEB_SEARCH_RESULT_SNIPPET_CHARS',
        'WEB_SEARCH_SEARXNG_INSTANCES',
        # Vision tools (transcribe_image, generate_image)
        'OCR_TIMEOUT',
        'IMAGE_SUBPROCESS_TIMEOUT',
        # GPU-offloaded image tiers (FP8 + cpu offload on <16 GB cards) get a larger budget
        'IMAGE_SUBPROCESS_TIMEOUT_OFFLOAD',
        # User-defined model-line pin for generate_image ('auto'|'high'|'mid'|'low') -
        # lets the user pick a smaller/faster model even on big hardware (2026-09-20)
        'IMAGE_GEN_TIER_OVERRIDE',
        # Self-unpacking tool runtime setup budget (generate_image bootstrap: venv + pip install)
        'TOOLS_BOOTSTRAP_TIMEOUT_SEC',
        # DNA learning module (delivered app.dna.learning does 'from config import MODEL_NAME')
        'MODEL_NAME',
        # Browser automation (web_interact __init__ + connect fallback start)
        'CHROME_CDP_PORT',
        # python_exec tool (reads via _cfg() from globals injected by dynamic_loader)
        'FILE_EXEC_OUTPUT_TRUNCATE_CHARS',
        'FILE_EXEC_ERROR_TRUNCATE_CHARS',
        'FILE_EXEC_TIMEOUT',
    }

    # Locate config.py relative to SERVER root (app/providers/coolems/ -> local_ai/)
    server_root = _server_root()
    config_path = server_root / "config" / "config.py"

    if not config_path.is_file():
        logger.warning(f"[SERVER] Config file not found at {config_path}, no constants sent to CLIENT")
        return {}

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            source = f.read()

        tree = ast.parse(source)
        constants = {}

        for node in ast.iter_child_nodes(tree):
            # Handle plain assignment: X = value  (ast.Assign)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in NEEDED_CONSTANTS:
                        try:
                            value = _ast_node_to_python(node.value)
                            # Only include JSON-serializable values (no functions, classes, etc.)
                            json.dumps(value)  # Test serialization
                            constants[target.id] = value
                        except (ValueError, TypeError, KeyError):
                            logger.debug(f"[SERVER] Could not extract config constant '{target.id}'")

            # Handle ANNOTATED assignment: X: type = value  (ast.AnnAssign)
            # ALL constants in config/config.py use this form!
            elif isinstance(node, ast.AnnAssign) and node.target is not None:
                if isinstance(node.target, ast.Name) and node.target.id in NEEDED_CONSTANTS:
                    if node.value is not None:  # Guard against declarations without values (X: int = ...)
                        try:
                            value = _ast_node_to_python(node.value)
                            json.dumps(value)  # Test serialization
                            constants[node.target.id] = value
                        except (ValueError, TypeError, KeyError):
                            logger.debug(f"[SERVER] Could not extract config constant '{node.target.id}'")

        if constants:
            logger.info(f"[SERVER] Extracted {len(constants)} config constants for CLIENT tools")
        else:
            logger.warning("[SERVER] No config constants extracted (tools may fail)")

        return constants

    except Exception as e:
        logger.error(f"[SERVER] Failed to extract config constants: {e}", exc_info=True)
        return {}
