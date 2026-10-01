"""Special file endpoints (complex operations: zip download, folder open, file execution)."""

import asyncio
import os
import io
import sys
import tempfile
from typing import Dict
from fastapi import APIRouter, Query, HTTPException

from fastapi.responses import StreamingResponse

from .middleware import _safe_relative_path, _get_working_root_dir, logger

from app.keys import is_subprocess_allowed  # SERVER-delivered subprocess limitation gate (2026-10-01)


def register_special_endpoints(router: APIRouter):
    """Register complex file operation endpoints on the given router."""

    @router.post("/api/download-zip")
    async def download_zip(data: Dict):
        """Create a ZIP from multiple paths and stream it as download.zip."""
        try:
            import zipfile
            paths = data.get("paths", [])
            if not paths:
                raise HTTPException(status_code=400, detail="No paths provided")
            working_root_dir = _get_working_root_dir()
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel_path in paths:
                    safe = _safe_relative_path(working_root_dir, rel_path)
                    if safe is None or not os.path.exists(safe):
                        continue
                    if os.path.isfile(safe):
                        zf.write(safe, rel_path)
                    elif os.path.isdir(safe):
                        for root, dirs, files in os.walk(safe):
                            for fname in files:
                                full_file = os.path.join(root, fname)
                                arcname = os.path.relpath(full_file, working_root_dir)
                                zf.write(full_file, arcname)
            zip_buffer.seek(0)
            return StreamingResponse(zip_buffer, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=download.zip"})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"ZIP download error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/open-folder")
    async def open_folder(path: str = Query("", description="Relative path inside working_root folder")):
        """Open the file/folder in system explorer."""
        try:
            import subprocess as subproc
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path) if path else working_root_dir
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            if not os.path.exists(safe_path):
                raise HTTPException(status_code=404, detail="Path not found")
            if sys.platform == "win32":
                os.startfile(safe_path)
            elif sys.platform == "darwin":
                subproc.Popen(["open", safe_path])
            else:
                subproc.Popen(["xdg-open", safe_path])
            return {"status": "opened"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Open folder error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/run-file")
    async def run_file(data: Dict):
        """Run a Python file. GUI apps (pygame, tkinter) launch in new terminal window.

        SERVER-side limitation gate (2026-10-01): before spawning any child process the CLIENT
        checks whether SUBPROCESS execution is allowed by the limitations the SERVER delivered for
        this machine's profile - per-profile python_exec_blocked_libs plus the per-role tool
        allowlist (app.keys.is_subprocess_allowed). No local role verification: whatever
        restrictions the SERVER sent over WebSocket are enforced here too, so a direct HTTP
        request cannot bypass what the agent channel is denied."""
        try:
            import subprocess as subproc
            allowed, reason = is_subprocess_allowed()
            if not allowed:
                raise HTTPException(status_code=403, detail=reason)
            path = data.get("path", "").strip()
            if not path:
                raise HTTPException(status_code=400, detail="Missing path parameter")
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            if not os.path.isfile(safe_path):
                raise HTTPException(status_code=404, detail="File not found")
            python_exe = sys.executable or "python"
            try:
                with open(safe_path, "r", encoding="utf-8", errors="ignore") as fcheck:
                    file_content = fcheck.read(4096)
            except Exception:
                file_content = ""
            gui_keywords = ["pygame", "tkinter", "PyQt", "wxPython", "pyside"]
            is_gui_app = any(kw in file_content.lower() for kw in gui_keywords)
            if sys.platform == "win32":
                if is_gui_app:
                    bat_path = os.path.join(tempfile.gettempdir(), f"run_py_{os.path.basename(safe_path)}.bat")
                    safe_dir = os.path.dirname(safe_path)
                    escaped_exe = python_exe.replace('"', '\\\"')
                    escaped_path = safe_path.replace('"', '\\\"')
                    bat_content = '@echo off\nchcp 65001 >nul\ncd /d "{safe_dir}"\n"{escaped_exe}" "{escaped_path}"\nif errorlevel 1 echo.\npause\n'.format(safe_dir=safe_dir, escaped_exe=escaped_exe, escaped_path=escaped_path)
                    with open(bat_path, "w", encoding="utf-8") as bf:
                        bf.write(bat_content)
                    subproc.Popen(["cmd.exe", "/c", bat_path], creationflags=subproc.CREATE_NEW_CONSOLE)
                    return {"status": "launched", "stdout": f"GUI application launched in new terminal window.\nFile: {os.path.basename(safe_path)}\nCheck your desktop for the game window.", "stderr": "", "returncode": 0}
                else:
                    startup_info = subproc.STARTUPINFO()
                    startup_info.dwFlags |= subproc.STARTF_USESHOWWINDOW
                    startup_info.wShowWindow = subproc.SW_HIDE
                    try:
                        # FIX (2026-08-29): blocking subprocess off the event loop - it used to freeze
                        # all HTTP endpoints (incl. /api/tree -> "Error loading files") for up to 30s.
                        result = await asyncio.to_thread(subproc.run, [python_exe, safe_path], capture_output=True, text=True, timeout=30, startupinfo=startup_info, cwd=os.path.dirname(safe_path))
                    except subproc.TimeoutExpired as e:
                        return {"stdout": (e.stdout or "").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), "stderr": (e.stderr or "Execution timed out after 30 seconds").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "Execution timed out after 30 seconds"), "returncode": -1, "status": "timeout"}
                    return {"stdout": result.stdout or "", "stderr": result.stderr or "", "returncode": result.returncode}
            else:
                try:
                    # FIX (2026-08-29): blocking subprocess off the event loop.
                    result = await asyncio.to_thread(subproc.run, [python_exe, safe_path], capture_output=True, text=True, timeout=30, cwd=os.path.dirname(safe_path))
                except subproc.TimeoutExpired as e:
                    return {"stdout": (e.stdout or "").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), "stderr": (e.stderr or "Execution timed out after 30 seconds").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "Execution timed out after 30 seconds"), "returncode": -1, "status": "timeout"}
                return {"stdout": result.stdout or "", "stderr": result.stderr or "", "returncode": result.returncode}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Run file error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
