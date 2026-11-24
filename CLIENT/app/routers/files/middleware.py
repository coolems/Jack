"""Shared helpers for file router endpoints.

Every files-router endpoint operates on ONE workspace folder, and that folder is decided
here, per request, by the router-level dependency ``resolve_file_working_root``:

    conversation_id (query) or X-Conversation-Id (header)
        -> ownership verdict        (app.workspace_access.require_workspace_access)
        -> that workspace's folder  (published to _file_request_working_root)

There is NO fallback folder: a request without a workspace scope, for an unknown
workspace, for a workspace the caller does not own, or for a workspace whose folder has
vanished is refused and reported as a security event. The process-global working root is
deliberately NOT consulted for file requests - it is whatever workspace was activated last
by ANY client, so using it here would hand out another workspace's files to a caller that
simply omits the id.
"""

import contextvars
import os
from datetime import datetime
import logging

from fastapi import HTTPException, Request

from app import security_events

logger = logging.getLogger("COOLEMS")

MISSING_SCOPE_DETAIL = "conversation_id is required - file access is scoped to one workspace"

_file_request_working_root = contextvars.ContextVar("file_request_working_root", default=None)
_file_request_workspace_id = contextvars.ContextVar("file_request_workspace_id", default=None)


def _request_facts(request) -> dict:
    """Method + path of the request being judged, for the security event."""
    return {
        "method": getattr(request, "method", None),
        "path": getattr(getattr(request, "url", None), "path", None),
    }


async def resolve_file_working_root(request: Request) -> None:
    """Authorize this request's workspace, then publish that workspace's folder.

    Reads ``conversation_id`` from the query string first, then the
    ``x-conversation-id`` header (set by the UI fetch override in static/ui/api-key.js).

    Refuses (before any endpoint body runs, so read, write, delete, zip, run-python and
    open-folder are all covered):
        400 - no workspace id claimed
        404 - not a workspace id, or no such workspace
        403 - the workspace belongs to another client
        409 - the workspace's stored folder no longer exists

    Kept ``async`` on purpose: a SYNC (def) dependency runs in the threadpool, and a
    ContextVar written there would not be visible to the endpoint - the folder would
    silently be None for every request.
    """
    claimed = (
        request.query_params.get("conversation_id")
        or request.headers.get("x-conversation-id")
        or ""
    ).strip()

    from app.helpers import get_client_ip

    client_ip = get_client_ip(request)

    if not claimed:
        security_events.record(
            "workspace_scope_missing", client_ip=client_ip, **_request_facts(request)
        )
        raise HTTPException(status_code=400, detail=MISSING_SCOPE_DETAIL)

    # Imported here: the router is built during app assembly, before the DB manager exists.
    from app.db_manager import get_db_connection, close_db_connection
    from app.utils.common import WorkspaceFolderMissing, resolve_working_root_from_row
    from app.workspace_access import require_workspace_access

    conn = get_db_connection()
    try:
        _, stored_root = require_workspace_access(
            conn.cursor(), claimed, client_ip, surface="files"
        )
    finally:
        close_db_connection(conn)

    try:
        # One query answered both questions: may this client touch the workspace, and
        # which folder is it.
        root = resolve_working_root_from_row(stored_root, claimed)
    except WorkspaceFolderMissing as missing:
        security_events.record(
            "workspace_folder_missing",
            client_ip=client_ip,
            workspace=claimed,
            working_root=str(missing),
            **_request_facts(request),
        )
        raise HTTPException(status_code=409, detail=f"Workspace folder does not exist: {missing}")

    _file_request_working_root.set(root)
    _file_request_workspace_id.set(claimed)


def request_workspace_id():
    """The workspace id the dependency authorized for the current request, or None."""
    return _file_request_workspace_id.get()


def _safe_relative_path(base: str, rel: str):
    """Compute a safe full path under base, rejecting traversal escapes.

    Uses realpath to resolve symlinks and .. sequences before the containment check.
    Returns None if the resolved path escapes *base* - and records the attempt, because a
    relative path that leaves the workspace folder is never a legitimate request.
    """
    if not rel:
        return base
    full = os.path.realpath(os.path.join(base, rel))
    base_real = os.path.realpath(base)
    if not (full.startswith(base_real + os.sep) or full == base_real):
        security_events.record("path_escape", requested=rel, base=base)
        return None
    return full


def _is_working_root_base(base: str, candidate: str) -> bool:
    """True when *candidate* resolves to *base* itself, not to something inside it.

    _safe_relative_path() guarantees containment, and containment INCLUDES the base
    which is right for listings and uploads, but never for a delete target: wiping the
    whole working root must be impossible. realpath() on both sides also catches the
    spellings that look like children but are not ('.', './', 'app/..', whitespace-only
    values, trailing spaces/dots that Windows realpath strips) and symlinks pointing back
    at the root. normcase() keeps the compare correct on case-insensitive drives.
    """
    return os.path.normcase(os.path.realpath(candidate)) == os.path.normcase(os.path.realpath(base))


def _get_working_root_dir():
    """Return the workspace folder the dependency authorized for THIS request.

    No fallback: every files endpoint runs behind resolve_file_working_root, so an empty
    ContextVar means the endpoint was reached without authorization - a programming error
    or an attempt to bypass the router dependency. Both are recorded and refused.
    """
    working_root = _file_request_working_root.get()
    if not working_root:
        security_events.record("working_root_unscoped")
        raise RuntimeError(
            "Working root was not resolved for this request - the files router "
            "dependency did not authorize a workspace"
        )
    if not os.path.isdir(working_root):
        security_events.record("workspace_folder_missing", working_root=working_root)
        raise RuntimeError(f"working_root is not a valid directory: {working_root!r}")
    return working_root


def _format_datetime(timestamp):
    """Format a file modification timestamp."""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
