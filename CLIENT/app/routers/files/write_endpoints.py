"""Write file endpoints (POST/DELETE operations)."""

import asyncio
import os
import shutil
import subprocess
import sys
import uuid
from typing import Dict
from fastapi import APIRouter, File, UploadFile, Query, HTTPException, Request


from app.utils.common import is_text_file, is_image_file, read_text_file, MAX_FILE_SIZE, get_working_root
from .middleware import _safe_relative_path, _get_working_root_dir, logger

from app.keys import is_subprocess_allowed  # SERVER-delivered subprocess limitation gate (2026-10-01)


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
        filepath = os.path.join(working_root_dir, safe_filename)
        with open(filepath, "wb") as f:
            f.write(content)
        result = {"url": f"/files/{safe_filename}", "filename": file.filename, "size": len(content)}
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
        """Delete a file or folder (recursively) by relative path."""
        try:
            if not path:
                raise HTTPException(status_code=400, detail="Missing path parameter")
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
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

        SERVER-side limitation gate (2026-10-01): before spawning any child process the CLIENT
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
        """Set the working root (UI action) - persists it to CLIENT/config/.working_root.json.

        SINGLE SOURCE OF TRUTH: this file is the only place the value lives.
        Every consumer (file endpoints, path guards, tools on SERVER and CLIENT)
        resolves through that same file via get_working_root(), so writing it
        here takes effect everywhere immediately - no propagation needed.
        """
        # No role gate (2026-09-30): changing the working root is a regular UI action available
        # to ANY caller/role at any time - no admin restriction.

        try:
            from app.utils.common import set_working_root as _set_wr
            new_path = data.get("working_root", "").strip()
            if not new_path:
                return {"success": False, "message": "No path provided"}
            if not os.path.isdir(new_path):
                return {"success": False, "message": f"Directory does not exist: {new_path}"}

            _set_wr(os.path.abspath(new_path))
            wr = get_working_root()
            logger.info(f"Working root updated to: {wr}")


            # Per-chat working root (2026-08-31): persist the new folder onto the
            # conversation that is open in the UI right now, so switching away and
            # back restores it. The UI sends 'conversation_id' alongside the path;
            # when absent or unknown this is a no-op (legacy behaviour).
            conv_id = str(data.get("conversation_id") or "").strip()
            if conv_id:
                try:
                    from app.websocket.db_ops import sync_conversation_working_root
                    sync_conversation_working_root(conv_id)
                except Exception as e:
                    logger.warning(f"Could not persist working_root for conversation {conv_id}: {e}")

            return {"success": True, "working_root": wr}

        except Exception as e:
            logger.error(f"Working root SET error: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return {"success": False, "message": str(e)}
