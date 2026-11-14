"""Special file endpoints (complex operations: zip download, folder open, file execution)."""

import os
import io
import sys
from typing import Dict
from fastapi import APIRouter, Query, HTTPException

from fastapi.responses import StreamingResponse

from .middleware import _safe_relative_path, _get_working_root_dir, logger

from app.keys import is_subprocess_allowed  # SERVER-delivered subprocess limitation gate (2026-10-01)

# Sandboxed execution seam (SECURITY 2026-11-14): /api/run-file runs INSIDE the python_exec
# runtime sandbox (both the captured branch and the GUI/console branch) instead of a bare
# subprocess.run(). Fail-closed: if the sandbox cannot be resolved the endpoint refuses (403).
from .sandbox_runner import (
    run_sandboxed_file, run_sandboxed_detached, SandboxUnavailable, _unavailable_detail,
)


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
        """Open a FOLDER in the system explorer (directories only).

        SERVER-side limitation gate (2026-10-01): same as /api/run-file and /api/run-python -
        spawning os.startfile / open / xdg-open is a child-process execution surface, so it
        checks is_subprocess_allowed() first.

        Directories only (2026-10-01): on Windows os.startfile() EXECUTES files (.bat/.exe/...),
        and /api/upload keeps the basename - an un-gated startfile on a file inside working_root
        would be a bypass of the exec policy. isdir() closes that surface.
        """
        try:
            import subprocess as subproc
            allowed, reason = is_subprocess_allowed()
            if not allowed:
                raise HTTPException(status_code=403, detail=reason)
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path) if path else working_root_dir
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            # Directories only - os.startfile() would EXECUTE a file (see docstring).
            if not os.path.isdir(safe_path):
                raise HTTPException(status_code=404, detail="Path is not a folder")
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
        """Run a Python file. GUI apps (pygame, tkinter) launch in a new terminal window.

        SECURITY (2026-11-14): BOTH branches now run INSIDE the python_exec runtime
        sandbox (app/routers/files/sandbox_runner.py). The previous implementation spawned
        ``subprocess.run([python_exe, safe_path])`` (and CREATE_NEW_CONSOLE for GUI apps)
        with NO containment, so any uploaded .py could read/write the whole machine and
        reach the network. The sandbox pins filesystem/spawn/socket decisions to
        working_root; if it cannot be resolved the request is REFUSED (403) - there is no
        unsandboxed fallback. The GUI branch keeps its own console window but carries the
        same bootstrap prefix and env pins.

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
            try:
                with open(safe_path, "r", encoding="utf-8", errors="ignore") as fcheck:
                    file_content = fcheck.read(4096)
            except Exception:
                file_content = ""
            gui_keywords = ["pygame", "tkinter", "PyQt", "wxPython", "pyside"]
            is_gui_app = any(kw in file_content.lower() for kw in gui_keywords)
            try:
                if is_gui_app:
                    # GUI apps need a real console window: detached spawn, SAME sandbox
                    # prefix and env pins as the captured branch (2026-11-14).
                    launched = await run_sandboxed_detached(safe_path, working_root_dir)
                    return {"status": "launched",
                            "stdout": f"GUI application launched in new terminal window.\nFile: {os.path.basename(safe_path)}\nCheck your desktop for the game window.",
                            "stderr": "", "returncode": 0, "pid": launched.get("pid")}
                try:
                    # Blocking child off the event loop (2026-08-29 fix: a blocking spawn
                    # froze every HTTP endpoint for up to 30s).
                    result = await run_sandboxed_file(safe_path, working_root_dir, timeout_sec=30)
                except subproc.TimeoutExpired as e:
                    return {"stdout": (e.stdout or "").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                            "stderr": (e.stderr or "Execution timed out after 30 seconds").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "Execution timed out after 30 seconds"),
                            "returncode": -1, "status": "timeout"}
                return {"stdout": result["stdout"] or "", "stderr": result["stderr"] or "",
                        "returncode": result["returncode"]}
            except SandboxUnavailable as e:
                raise HTTPException(status_code=403, detail=_unavailable_detail(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Run file error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
