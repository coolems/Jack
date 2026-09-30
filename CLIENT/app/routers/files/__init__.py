"""Files router package - split from monolithic files.py for better maintainability."""

from fastapi import APIRouter, HTTPException

from .middleware import logger


def create_files_router() -> APIRouter:
    """Factory function that returns a configured files router with all endpoints.

    Backward compatible with the original single-file implementation.
    """
    from .read_endpoints import register_read_endpoints
    from .write_endpoints import register_write_endpoints
    from .special_endpoints import register_special_endpoints

    router = APIRouter(tags=["files"])
    # Register all endpoint groups
    register_read_endpoints(router)
    register_write_endpoints(router)
    register_special_endpoints(router)
    return router

# Re-export for backward compatibility with direct imports
__all__ = ["create_files_router"]
