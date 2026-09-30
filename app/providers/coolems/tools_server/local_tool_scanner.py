"""Dynamically discovers all tool .py files from SERVER disk into memory cache.

Zero hardcoded paths — uses os.listdir() to find everything under tools/ folder.
Scans once at startup and caches ToolSource objects in memory.
"""

import ast
import json
import os
import logging
from typing import Dict, List, Optional
from pathlib import Path

from .tool_source import ToolSource

logger = logging.getLogger(__name__)


class LocalToolScanner:
    """Auto-discovers all tools from SERVER's tools/ directory."""

    def __init__(self, tools_dir: str):
        self.tools_dir = Path(tools_dir)
        self._registry: Dict[str, ToolSource] = {}
        self._shared_sources: Optional[Dict[str, str]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> int:
        """Scan tools/ directory and populate in-memory registry.

        Returns number of tools discovered.
        """
        if not self.tools_dir.exists():
            logger.warning("Tools directory does not exist: %s", self.tools_dir)
            return 0

        # Load shared modules first (all root-level .py files that are NOT tool folders)
        self._load_shared_modules()

        # Walk through all subdirectories looking for .py files with __tool_description__
        count = 0
        for folder_name in sorted(os.listdir(self.tools_dir)):
            folder_path = self.tools_dir / folder_name
            if not folder_path.is_dir():
                continue
            count += self._scan_folder(folder_name, folder_path)

        logger.info("Tool scan complete: %d tools discovered", len(self._registry))
        return len(self._registry)

    def get_tool(self, name: str) -> Optional[ToolSource]:
        """Retrieve a tool by name from the cache."""
        return self._registry.get(name)

    def list_tools(self) -> List[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._registry.keys())

    def get_all_definitions(self) -> List[Dict]:
        """Return definitions for ALL registered tools."""
        return [t.to_dict() for t in self._registry.values()]

    def get_allowed_definitions(self, allowed_names: List[str]) -> List[Dict]:
        """Return definitions only for permitted tool names."""
        defs = []
        for name in allowed_names:
            tool = self._registry.get(name)
            if tool is not None:
                defs.append(tool.to_dict())
        return defs

    @property
    def shared_utils_source(self) -> Optional[str]:
        """Legacy property — returns utils.py source only (for backward compat)."""
        if self._shared_sources:
            return self._shared_sources.get('utils')
        return None

    @property
    def all_shared_sources(self) -> Dict[str, str]:
        """Return ALL shared module sources as a dict.
        
        Keys are module names (e.g., 'utils', 'path_guard'), values are source strings.
        None values indicate the module was not found on disk.
        """
        return dict(self._shared_sources) if self._shared_sources else {}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_shared_modules(self):
        """Load ALL root-level .py files from tools/ as shared modules.
        
        This includes utils.py, path_guard.py, shared_browser.py, etc.
        These are the modules that tool code imports via 'from tools.XXX import YYY'.
        They get sent to CLIENT and installed in-memory (never on disk).
        """
        self._shared_sources = {}

        # Known shared module files — root-level .py files under tools/
        # We explicitly list these rather than blindly loading everything
        # to avoid accidentally sending __init__.py, tool_detection.py, etc.
        known_shared_modules = [
            'utils',           # get_working_root, resolve_path_to_dir, validate_filename, etc.
            'path_guard',      # guard_path_inside_working_root, secure_find_file, is_safe_path, etc.
            'shared_browser',  # shared browser instance for web tools
            'ssrf_guard',      # SSRF defense v2 (2026-09): policy + PinnedConnector/safe_session
                                # MUST stay before ssrf_defense (the shim imports from it)
            'ssrf_defense',    # DEPRECATED shim re-exporting tools.ssrf_guard (legacy tool code)
            'tool_bootstrap',  # generic self-unpacking runtime bootstrap (venv + worker) for content-gen tools
        ]

        for mod_name in known_shared_modules:
            module_path = self.tools_dir / f"{mod_name}.py"
            if module_path.exists():
                try:
                    source = module_path.read_text(encoding="utf-8")
                    self._shared_sources[mod_name] = source
                    logger.info("Loaded shared module '%s' (%d chars)", mod_name, len(source))
                except Exception as exc:
                    logger.error("Failed to load %s.py: %s", mod_name, exc)
                    self._shared_sources[mod_name] = None  # Mark as unavailable
            else:
                logger.warning("Shared module '%s' not found at %s — CLIENT tools that need it will fail",
                             mod_name, module_path)
                self._shared_sources[mod_name] = None

            # Load subpackage shared modules needed by compiled web_interact tools
            # These use 'from .X import Y' which dynamic_loader comments out
            known_subpackage_modules = [
                ('web_interact', 'shared_instance'),   # get_web_interact, set_web_interact
                ('web_interact', 'ensure_connected'),  # ensure_connected helper
            ]
            for pkg_name, mod_name in known_subpackage_modules:
                full_key = f'{pkg_name}.{mod_name}'
                module_path = self.tools_dir / pkg_name / f"{mod_name}.py"
                if module_path.exists():
                    try:
                        source = module_path.read_text(encoding="utf-8")
                        self._shared_sources[full_key] = source
                        logger.info("Loaded subpackage shared '%s' (%d chars)", full_key, len(source))
                    except Exception as exc:
                        logger.error("Failed to load %s/%s.py: %s", pkg_name, mod_name, exc)
                        self._shared_sources[full_key] = None
                else:
                    logger.warning("Subpackage module '%s' not found at %s", full_key, module_path)
                    self._shared_sources[full_key] = None

    
    def _scan_folder(self, folder_name: str, folder_path: Path) -> int:
        """Scan a single subdirectory for tool files."""
        count = 0
        try:
            for filename in sorted(os.listdir(folder_path)):
                if not filename.endswith(".py"):
                    continue
                filepath = folder_path / filename
                self._try_load_tool(folder_name, filename, filepath)
                count += 1
        except Exception as exc:
            logger.error("Error scanning folder %s: %s", folder_name, exc)
        return count

    def _try_load_tool(self, folder: str, filename: str, filepath: Path):
        """Try to extract __tool_description__ definition from a .py file."""
        try:
            source_code = filepath.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Cannot read %s/%s: %s", folder, filename, exc)
            return

        definition = self._extract_definition(source_code)
        if definition is None:
            # Not a tool file (e.g. __init__.py, helpers.py)
            return

        name = definition.get("name", filename[:-3])  # fallback to filename without .py
        dependencies = self._detect_dependencies(source_code)

        tool = ToolSource(
            name=name,
            folder=folder,
            filename=filename,
            definition=definition,
            source_code=source_code,
            dependencies=dependencies,
            manifest=self._load_manifest(folder_path),
        )
        self._registry[name] = tool
        logger.debug("Registered tool: %s from %s/%s", name, folder, filename)

    def _safe_ast_to_value(self, node):
        """Safely convert an AST node to a Python value without eval().

        Only allows literal values: str, int, float, bool, None,
        dict, list, tuple. Rejects calls, attributes, and other expressions.
        """
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, (ast.Str, ast.Num)):  # Python 3.7 compat
            return node.s if isinstance(node, ast.Str) else node.n
        elif isinstance(node, ast.NameConstant):  # Python 3.7 compat for True/False/None
            return node.value
        elif isinstance(node, ast.Dict):
            keys = [self._safe_ast_to_value(k) for k in node.keys]
            vals = [self._safe_ast_to_value(v) for v in node.values]
            return dict(zip(keys, vals))
        elif isinstance(node, ast.List):
            return [self._safe_ast_to_value(item) for item in node.elts]
        elif isinstance(node, ast.Tuple):
            return tuple(self._safe_ast_to_value(item) for item in node.elts)
        else:
            raise ValueError(
                f"Unsupported expression type {type(node).__name__} "
                "in __tool_description__ definition (only literals allowed)"
            )

    def _extract_definition(self, source_code: str) -> Optional[dict]:
        """Extract the __tool_description__ dict definition using AST parsing."""
        try:
            tree = ast.parse(source_code)
        except SyntaxError as exc:
            logger.warning("Syntax error in %s: %s", source_code[:50], exc)
            return None

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__tool_description__":
                        # Safely convert AST node to value without eval()
                        try:
                            result = self._safe_ast_to_value(node.value)
                            if isinstance(result, dict):
                                return result
                        except Exception as exc:
                            logger.warning("Failed to extract __tool_description__: %s", exc)
        return None

    def _detect_dependencies(self, source_code: str) -> List[str]:
        """Detect import statements that reference other tool modules."""
        deps = []
        try:
            tree = ast.parse(source_code)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith("tools."):
                        deps.append(module)
        except SyntaxError:
            pass  # Can't parse — no dependency info available
        return deps

    def _load_manifest(self, folder_path) -> Optional[dict]:
        """Load <folder>/tool_manifest.json if present (self-unpacking tools).

        The manifest tells the CLIENT how to unfold its own runtime for this tool
        (runtime folder name + pip requirements). Missing/invalid file -> None so
        plain in-memory tools are unaffected.
        """
        try:
            mpath = folder_path / "tool_manifest.json"
            if not mpath.is_file():
                return None
            data = json.loads(mpath.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                logger.info("Loaded tool manifest for %s (v%s)",
                            data.get("name", folder_path.name), data.get("version", "?"))
                return data
        except Exception as exc:
            logger.warning("Could not load tool_manifest.json in %s: %s", folder_path, exc)
        return None
