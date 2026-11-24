"""Files router package: upload, tree, download and file read/write endpoints.

The endpoint groups live in read_endpoints.py, write_endpoints.py and
special_endpoints.py; create_files_router() assembles them onto one APIRouter.
"""

from fastapi import APIRouter, Depends

from .middleware import resolve_file_working_root


def create_files_router() -> APIRouter:
    """Factory returning a configured files router with all endpoints attached."""
    from .read_endpoints import register_read_endpoints
    from .write_endpoints import register_write_endpoints
    from .special_endpoints import register_special_endpoints

    # The router-level dependency resolves the workspace folder PER REQUEST from the
    # request's conversation_id (query param or x-conversation-id header) before any
    # endpoint runs. _get_working_root_dir() then reads that request-scoped value
    # instead of the process-global _WORKING_ROOT_CACHE, so two tabs on two
    # workspaces no longer race the Files panel.
    router = APIRouter(
        tags=["files"],
        dependencies=[Depends(resolve_file_working_root)],
    )
    # Register all endpoint groups
    register_read_endpoints(router)
    register_write_endpoints(router)
    register_special_endpoints(router)
    return router

# Re-export for backward compatibility with direct imports
__all__ = ["create_files_router"]
