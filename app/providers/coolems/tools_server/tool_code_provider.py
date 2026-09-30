"""Tool code provider — permission-checked delivery of tool source to CLIENT."""

import logging
from typing import Dict, List, Optional

from .local_tool_scanner import LocalToolScanner

logger = logging.getLogger("COOLEMS.Tools.Server.Provider")


class ToolCodeProvider:
    """Mediates between authenticated clients and the in-memory tool cache.

    Enforces role-based permissions before delivering any source code.
    All responses are plain dicts ready for JSON serialization over WebSocket.
    """

    def __init__(self, scanner: LocalToolScanner):
        self._scanner = scanner

    # ------------------------------------------------------------------
    # Public API — called from server_provider message handlers
    # ------------------------------------------------------------------

    def handle_tools_request(self, user_info: Dict) -> Dict:
        """Handle 'tools_request' from CLIENT. Returns definitions + ALL shared modules."""
        allowed_names = self._resolve_allowed_tools(user_info)

        if allowed_names is None:
            defs = self._scanner.get_all_definitions()
        else:
            defs = self._scanner.get_allowed_definitions(allowed_names)

        # Send ALL shared module sources (utils, path_guard, etc.) as a dict
        all_shared = self._scanner.all_shared_sources

        logger.info(f"tools_request: sending {len(defs)} definitions to role={user_info.get('role')}")
        return {
            "type": "tools_response",
            "definitions": defs,
            "allowed_tools": allowed_names,
            # Legacy field for backward compat (single-string utils only)
            "shared_utils_source": all_shared.get('utils'),
            # NEW: dict of ALL shared module sources keyed by module name
            "shared_sources": {k: v for k, v in all_shared.items() if v is not None},
        }

    def handle_tool_code_request(self, tool_name: str, user_info: Dict) -> Dict:
        """Handle 'tool_code_request' from CLIENT. Returns source code if permitted."""
        # Permission check
        allowed_names = self._resolve_allowed_tools(user_info)
        if allowed_names is not None and tool_name not in allowed_names:
            logger.warning(f"tool_code_request DENIED: '{tool_name}' not allowed for role={user_info.get('role')}")
            return {
                "type": "tool_code_response",
                "error": f"Tool '{tool_name}' is not permitted for your role.",
            }

        tool = self._scanner.get_tool(tool_name)
        if tool is None:
            logger.warning(f"tool_code_request MISSING: '{tool_name}'")
            return {
                "type": "tool_code_response",
                "error": f"Tool '{tool_name}' not found on server.",
            }

        logger.info(f"tool_code_request OK: '{tool_name}' -> role={user_info.get('role')}")
        return {
            "type": "tool_code_response",
            **tool.to_code_response(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_allowed_tools(self, user_info: Dict) -> Optional[List[str]]:
        """Return allowed tool names from user profile, or None for unrestricted."""
        return user_info.get("allowed_tools")  # None = all tools permitted
