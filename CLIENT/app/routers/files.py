"""Backward compatibility shim - delegates to the new files/ package."""
from .files import create_files_router  # noqa: F401

# Re-export for direct imports from this module
__all__ = ["create_files_router"]
