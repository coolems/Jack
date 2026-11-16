"""Files router package: upload, tree, download and file read/write endpoints.

The endpoint groups live in read_endpoints.py, write_endpoints.py and
special_endpoints.py; create_files_router() assembles them onto one APIRouter.
"""

from fastapi import APIRouter


def create_files_router() -> APIRouter:
    """Factory returning a configured files router with all endpoints attached."""
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
