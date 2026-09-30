"""
CLIENT tools module - REMOTE-ONLY architecture.

All tool source code lives on SERVER. This module provides the mechanism to:
  1. Fetch tool definitions + source from SERVER via WebSocket
  2. Compile into live functions in memory (AST security validated)
  3. Execute locally with working_root injected at runtime
  4. Cache compiled tools in LRU cache (never written to disk)

Zero tool code is stored on CLIENT disk. Everything comes from SERVER on-demand.
"""

from .remote_tools import RemoteToolOrchestrator, ToolCache, ToolFetcher
from .remote_tools.dynamic_loader import DynamicModuleLoader, ToolCompilationError

__all__ = [
    "RemoteToolOrchestrator",
    "ToolCache",
    "ToolFetcher",
    "DynamicModuleLoader",
    "ToolCompilationError",
]
