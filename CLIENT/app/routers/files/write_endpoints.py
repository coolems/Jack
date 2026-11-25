"""Write file endpoints (POST/DELETE operations)."""

import asyncio
import os
import shutil
import subprocess
import sys
import uuid
from typing import Dict
from fastapi import APIRouter, File, UploadFile, Query, HTTPException, Request


from app.utils.common import is_text_file, is_image_file, read_text_file, MAX_FILE_SIZE
from .middleware import _safe_relative_path, _is_working_root_base, _get_working_root_dir, logger

from app.keys import is_subprocess_allowed  # SERVER-delivered subprocess limitation gate (2026-10-01)

# Windows device names cannot be used as a file or folder name (with or without an
# extension): 'nul.txt' still addresses the NUL device.
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def register_write_endpoints(router: APIRouter):
    """Register all POST/DELETE file modification endpoints on the given router."""

    @router.post("/api/upload")
    async def upload_file(file: UploadFile = File(...)):
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="File too large")
        file_id = str(uuid.uuid4())
        # SECURITY (2026-07-15): multipart filenames are attacker-controlled --
        # strip any directory part so the name can never escape working_root.
        upload_name = os.path.basename((file.filename or "upload.bin").replace("\\", "/")) or "upload.bin"
        safe_filename = f"{file_id}_{upload_name}"
        working_root_dir = _get_working_root_dir()
        # Prompt-window attachments (drag/drop, paste, file picker) land in
        # <working_root>/.temp/user_files/ - never in the working_root root.
        # .temp/ is the standard scratch space (git-ignored), so attachments
        # never reach GitHub.
        upload_dir = os.path.join(working_root_dir, ".temp", "user_files")
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, safe_filename)
        with open(filepath, "wb") as f:
            f.write(content)
        result = {"url": f"/files/.temp/user_files/{safe_filename}", "filename": file.filename, "size": len(content)}
        if is_text_file(file.filename):
            text_content = read_text_file(filepath)
            result["is_text"] = True
            result["content"] = text_content
        elif is_image_file(file.filename):
            result["is_image"] = True
            logger.info(f"Image uploaded: {file.filename} ({len(content)} bytes)")
        return result

    @router.delete("/api/tree")
    async def delete_tree(path: str = Query(..., description="Relative path inside working_root folder")):
        """Delete a file or folder (recursively) by relative path.

        The target must be a real CHILD of working_root. _safe_relative_path() only
        guarantees containment, and containment includes the base itself - '.', './',
        'app/..' and whitespace-only values all resolve to the working root, which would
        reach shutil.rmtree() and wipe the whole workspace (the Files tab deletes without
        a confirmation dialog). An empty/blank path is a missing parameter, and a path
        that resolves to the root is refused outright.
        """
        try:
            if not path.strip():
                raise HTTPException(status_code=400, detail="Missing path parameter")
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            if _is_working_root_base(working_root_dir, safe_path):
                from app import security_events
                security_events.record("working_root_delete_blocked", requested=path)
                raise HTTPException(status_code=400, detail="Refusing to delete the working root itself")
            if not os.path.exists(safe_path):
                raise HTTPException(status_code=404, detail="Path not found")
            if os.path.isdir(safe_path):
                shutil.rmtree(safe_path)
                logger.info(f"Deleted folder: {path}")
            else:
                os.remove(safe_path)
                logger.info(f"Deleted file: {path}")
            return {"status": "deleted"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Delete error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/api/working-root-files/{filename}")
    async def delete_working_root_file(filename: str):
        try:
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, filename)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path - access denied")
            if not os.path.isfile(safe_path):
                raise HTTPException(status_code=404, detail="File not found")
            os.remove(safe_path)
            logger.info(f"Deleted file from working root: {os.path.basename(safe_path)}")
            return {"status": "deleted"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Delete working root file error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/move")
    async def move_item(data: Dict):
        """Move a file or a folder (recursively) to another folder of the workspace.

        Used by the Files panel: dragging a row onto a folder, and the row menu's
        "Move to folder...". Body: {"source": "a/b.txt", "destination_path": "c"} where
        destination_path is a DIRECTORY ('' = the workspace root).

        Refused (400/404/409) instead of guessed:
          * a blank source, or a source that resolves to the workspace root itself
            (moving the root into the root would delete the workspace),
          * a destination outside the workspace, or one that is not a folder,
          * a folder moved into itself or into one of its own subfolders,
          * an existing target (this endpoint never overwrites - a move that silently
            replaced a file would lose data),
          * a source that does not exist.

        shutil.move() is used, so a folder arrives with its whole subtree, and a move
        across drives degrades to copy+delete instead of failing.
        """
        try:
            source = str(data.get("source") or "").strip()
            destination = str(data.get("destination_path") or "").strip()
            if not source:
                raise HTTPException(status_code=400, detail="Missing source")
            working_root_dir = _get_working_root_dir()
            safe_source = _safe_relative_path(working_root_dir, source)
            if safe_source is None:
                raise HTTPException(status_code=400, detail="Invalid source path")
            if _is_working_root_base(working_root_dir, safe_source):
                from app import security_events
                security_events.record("working_root_move_blocked", requested=source)
                raise HTTPException(status_code=400, detail="Refusing to move the working root itself")
            if not os.path.exists(safe_source):
                raise HTTPException(status_code=404, detail="Source not found")

            safe_dest_dir = _safe_relative_path(working_root_dir, destination) if destination else working_root_dir
            if safe_dest_dir is None:
                raise HTTPException(status_code=400, detail="Invalid destination path")
            if not os.path.isdir(safe_dest_dir):
                raise HTTPException(status_code=400, detail="Destination is not a folder")

            # A folder can never be moved inside itself: the destination would be
            # destroyed together with the source it now lives in.
            source_real = os.path.normcase(os.path.realpath(safe_source))
            dest_real = os.path.normcase(os.path.realpath(safe_dest_dir))
            if os.path.isdir(safe_source) and (dest_real == source_real
                                               or dest_real.startswith(source_real + os.sep)):
                raise HTTPException(status_code=400, detail="Cannot move a folder into itself")

            target = os.path.join(safe_dest_dir, os.path.basename(safe_source))
            if os.path.normcase(os.path.realpath(target)) == source_real:
                raise HTTPException(status_code=400, detail="Source is already in that folder")
            if os.path.exists(target):
                raise HTTPException(status_code=409, detail=f"'{os.path.basename(target)}' already exists there")

            # Blocking filesystem work off the event loop (a big folder move must not
            # freeze every other HTTP endpoint, same rule as the run-* endpoints).
            await asyncio.to_thread(shutil.move, safe_source, target)
            rel_target = os.path.relpath(target, working_root_dir).replace(os.sep, "/")
            logger.info(f"Moved {source} -> {rel_target}")
            return {"status": "moved", "path": rel_target}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Move error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/rename")
    async def rename_item(data: Dict):
        """Rename a file or a folder in place (the row menu's "Rename").

        Body: {"path": "a/b.txt", "new_name": "c.txt"}. The new name is a BARE name: it
        may not contain a separator, may not be '.'/'..' and may not be a reserved
        Windows device name, so a rename can never walk out of its folder. An existing
        target is refused (no overwrite). Only the case of a name may change on a
        case-insensitive filesystem - the old and new path are the same file there, so
        that case is allowed explicitly.
        """
        try:
            path = str(data.get("path") or "").strip()
            new_name = str(data.get("new_name") or "").strip()
            if not path:
                raise HTTPException(status_code=400, detail="Missing path")
            if not new_name:
                raise HTTPException(status_code=400, detail="Missing new_name")
            if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
                raise HTTPException(status_code=400, detail="A name must not contain a path separator")
            if new_name != os.path.basename(new_name):
                raise HTTPException(status_code=400, detail="A name must not contain a path separator")
            if sys.platform == "win32":
                stem = new_name.split(".")[0].strip().upper()
                if stem in _WINDOWS_RESERVED_NAMES:
                    raise HTTPException(status_code=400, detail=f"'{new_name}' is a reserved Windows name")

            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            if _is_working_root_base(working_root_dir, safe_path):
                raise HTTPException(status_code=400, detail="Refusing to rename the working root itself")
            if not os.path.exists(safe_path):
                raise HTTPException(status_code=404, detail="Path not found")

            target = os.path.join(os.path.dirname(safe_path), new_name)
            # Containment for the RESULT too: a name that resolves elsewhere (a symlink
            # inside the folder, an odd drive-relative spelling) is refused.
            if _safe_relative_path(working_root_dir, os.path.relpath(target, working_root_dir)) is None:
                raise HTTPException(status_code=400, detail="Invalid new name")
            same_file = os.path.normcase(os.path.realpath(target)) == os.path.normcase(os.path.realpath(safe_path))
            if not same_file and os.path.exists(target):
                raise HTTPException(status_code=409, detail=f"'{new_name}' already exists")

            if not same_file:
                await asyncio.to_thread(os.rename, safe_path, target)
            elif os.path.basename(safe_path) != new_name:
                # Case-only rename on a case-insensitive filesystem: the target "exists"
                # because it IS the source. Go through a temporary name so the change is
                # actually applied instead of being a no-op.
                temp = safe_path + ".renaming"
                await asyncio.to_thread(os.rename, safe_path, temp)
                await asyncio.to_thread(os.rename, temp, target)
            rel_target = os.path.relpath(target, working_root_dir).replace(os.sep, "/")
            logger.info(f"Renamed {path} -> {rel_target}")
            return {"status": "renamed", "path": rel_target}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Rename error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/upload-to-folder")
    async def upload_to_folder(file: UploadFile = File(...), folder_path: str = Query("", description="Relative path inside working_root folder")):
        """Upload a file directly into a specific folder."""
        try:
            content = await file.read()
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail="File too large")
            working_root_dir = _get_working_root_dir()
            if folder_path:
                safe_dir = _safe_relative_path(working_root_dir, folder_path)
                if safe_dir is None:
                    raise HTTPException(status_code=400, detail="Invalid folder path")
            else:
                safe_dir = working_root_dir
            os.makedirs(safe_dir, exist_ok=True)
            file_id = str(uuid.uuid4())
            # SECURITY (2026-07-15): multipart filenames are attacker-controlled --
            # strip any directory part so the name can never escape working_root.
            upload_name = os.path.basename((file.filename or "upload.bin").replace("\\", "/")) or "upload.bin"
            safe_filename = f"{file_id}_{upload_name}"
            filepath = os.path.join(safe_dir, safe_filename)
            with open(filepath, "wb") as f:
                f.write(content)
            rel_path = os.path.relpath(filepath, working_root_dir)
            result = {"url": f"/files/{rel_path.replace(os.sep, '/')}", "filename": file.filename, "size": len(content), "folder": folder_path}
            if is_text_file(file.filename):
                text_content = read_text_file(filepath)
                result["is_text"] = True
                result["content"] = text_content
            elif is_image_file(file.filename):
                result["is_image"] = True
            logger.info(f"File uploaded to folder: {file.filename} -> {folder_path or 'working_root/'}")
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Upload to folder error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/run-python")
    async def run_python_file(path: str = Query(..., description="Relative path inside working_root folder")):
        """Run a Python file and return its output.

        SERVER-side limitation gate: before spawning any child process the CLIENT
        checks whether SUBPROCESS execution is allowed by the limitations the SERVER delivered
        for this machine's profile - per-profile python_exec_blocked_libs plus the per-role tool
        allowlist (app.keys.is_subprocess_allowed). No local role verification: whatever
        restrictions the SERVER sent over WebSocket are enforced here too, so a direct HTTP
        request cannot bypass what the agent channel is denied.
        """
        try:
            allowed, reason = is_subprocess_allowed()
            if not allowed:
                raise HTTPException(status_code=403, detail=reason)
            if not path:
                raise HTTPException(status_code=400, detail="Missing path parameter")
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            if not os.path.isfile(safe_path):
                raise HTTPException(status_code=404, detail="File not found")
            # 'subprocess' is imported at module level (audit fix 2026-10-01): the outer
            # `except subprocess.TimeoutExpired` clause must resolve even when an error occurs
            # BEFORE any spawn (e.g. working_root resolution failure) - a local import inside
            # the try block left that name unbound in such cases and evaluating the except-chain
            # raised NameError instead of returning the real 500 with the actual cause.
            python_exe = sys.executable or "python"
            if sys.platform == "win32":
                startup_info = subprocess.STARTUPINFO()
                startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                # CPython's subprocess module only defines SW_HIDE (0) - it has never defined
                # SW_SHOWNORMAL, so direct attribute access raised AttributeError on every Windows
                # run. Use the raw Win32 value directly: SW_SHOWNORMAL == 1 (5 is SW_SHOW).
                startup_info.wShowWindow = 1
                # FIX (2026-08-29): blocking subprocess off the event loop - it used to freeze
                # all HTTP endpoints (incl. /api/tree -> "Error loading files") for up to 30s.
                result = await asyncio.to_thread(subprocess.run, [python_exe, safe_path], capture_output=True, text=True, timeout=30, startupinfo=startup_info)
            else:
                result = await asyncio.to_thread(subprocess.run, [python_exe, safe_path], capture_output=True, text=True, timeout=30)
            return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
        except HTTPException:
            raise
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=408, detail="Execution timed out")
        except Exception as e:
            logger.error(f"Run Python error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/working_root")
    async def set_working_root_endpoint(request: Request, data: Dict):
        """Activate this workspace's working folder (UI action) - per-workspace, DB-backed.
        An explicit path updates the workspace setting; no path re-activates its saved one.

        The workspace is ALWAYS the one the router-level dependency authorized: a body
        conversation_id that disagrees with the request scope is refused (and reported), and
        a request that carries no scope never gets here at all. Without that rule this
        endpoint was a way to re-point ARBITRARY workspaces, and - because the old code also
        moved the process-global folder - to hand another client's files to the next
        id-less request.
        """
        # No role gate (2026-09-30): changing the working root is a regular UI action available
        # to ANY caller/role at any time - no admin restriction.

        try:
            from app.utils.common import set_working_root
            from app.helpers import get_client_ip
            from app import security_events
            from .middleware import request_workspace_id

            scope = request_workspace_id()  # authorized for THIS request by the dependency
            new_path = str(data.get("working_root") or "").strip()
            claimed = str(data.get("conversation_id") or "").strip()

            if claimed and claimed != scope:
                security_events.record(
                    "workspace_scope_mismatch",
                    client_ip=get_client_ip(request),
                    request_scope=scope or "none",
                    body_scope=claimed,
                )
                raise HTTPException(
                    status_code=400,
                    detail="conversation_id in the body must match the request's workspace scope",
                )

            wr = set_working_root(new_path, scope)
            logger.info(f"Working root activated to: {wr} (conv={str(scope)[:8]})")

            return {"success": True, "working_root": wr}

        except HTTPException:
            raise  # 400/403/404/409 must reach the caller as-is
        except Exception as e:
            logger.error(f"Working root SET error: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return {"success": False, "message": str(e)}
