"""Tool source-code lookup by name, including same-package dependencies."""

import os
from .shared_sources import _read_text

from .log import logger
from .same_package_imports import _find_same_package_imports

def _locate_tool_file(tools_dir, tool_name):
    """Find <tools_dir>/<subdir>/<tool_name>.py. Returns (path, subdir) or (None, None)."""
    for item in os.listdir(tools_dir):
        item_path = os.path.join(tools_dir, item)
        if not os.path.isdir(item_path):
            continue

        file_path = os.path.join(item_path, f"{tool_name}.py")
        if os.path.exists(file_path):
            return file_path, item_path  # The subdirectory containing the tool
    return None, None

def get_tool_source_code_with_deps(tools_dir, tool_name):
    """Read source code for a specific tool AND its same-package dependencies.

    Returns dict with 'source_code' and 'shared_deps' (dict of dep_name -> source_code).

    Handles both regular same-package imports ('from .module import X')
    and bare parent package imports ('from . import ClassName as Alias').
    """
    result = {"source_code": None, "shared_deps": {}, "manifest": None}

    try:
        tool_path, tool_dir = _locate_tool_file(tools_dir, tool_name)

        if not tool_path:
            return result

        source = _read_text(tool_path)

        result["source_code"] = source

        # (2026-08-23) Self-unpacking tools ship their manifest so the CLIENT can create its own
        # <working_root>/tools/<name>/ runtime folder + venv.
        result["manifest"] = _load_tool_manifest(tool_dir, tool_name)

        # Find same-package imports in the source (including bare imports)
        deps = _find_same_package_imports(source, tool_name)

        if not deps:
            return result  # No dependencies found

        logger.info(f"[SERVER] Tool '{tool_name}' has {len(deps)} same-package dependency(ies): {list(deps.keys())}")

        # Read source code for each dependency (they're in the same directory)
        for dep_mod, _names in deps.items():
            if dep_mod == '__init__':
                # Bare import from parent package — read __init__.py
                dep_path = os.path.join(tool_dir, "__init__.py")
            else:
                dep_path = os.path.join(tool_dir, f"{dep_mod}.py")

            if os.path.exists(dep_path):
                dep_source = _read_text(dep_path)

                # Use the actual filename key for regular deps, '__init__' for bare imports
                result["shared_deps"][dep_mod] = dep_source
                logger.info(f"[SERVER] Included dependency '{dep_mod}' for tool '{tool_name}' ({len(dep_source)} chars)")
            else:
                logger.warning(f"[SERVER] Dependency file '{dep_mod}.py' not found in {tool_dir}")

    except Exception as e:
        logger.error(f"[SERVER] Error reading tool source with deps for {tool_name}: {e}", exc_info=True)

    return result

def get_tool_source_code(tools_dir, tool_name):
    """Read source code for a specific tool by name."""
    try:
        file_path, _ = _locate_tool_file(tools_dir, tool_name)
        if file_path is not None:
            return _read_text(file_path)
    except Exception as e:
        logger.error(f"[SERVER] Error reading tool source for {tool_name}: {e}")
    return None


def _load_tool_manifest(tool_dir: str, tool_name: str):
    """Load <tool_dir>/tool_manifest.json if present (self-unpacking tools).

    The manifest tells the CLIENT how to unfold its own runtime for this tool
    (runtime folder name + pip requirements). Missing/invalid file -> None so
    plain in-memory tools are unaffected.
    """
    import json as _json
    try:
        mpath = os.path.join(tool_dir, "tool_manifest.json")
        if not os.path.exists(mpath):
            return None
        with open(mpath, 'r', encoding='utf-8') as f:
            data = _json.load(f)
        if isinstance(data, dict):
            logger.info(f"[SERVER] Loaded tool manifest for '{tool_name}' (v{data.get('version', '?')}")
            return data
    except Exception as e:
        logger.warning(f"[SERVER] Could not load tool_manifest.json in {tool_dir} for '{tool_name}': {e}")
    return None
