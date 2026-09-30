"""Read-only file endpoints (GET operations) + compare/diff support."""

import os
import mimetypes
import hashlib
from typing import Dict
from fastapi import APIRouter, File, UploadFile, Query, HTTPException
from fastapi.responses import FileResponse

from app.utils.common import is_text_file, read_text_file, get_working_root, FILE_MAX_WRITE_BYTES
from .middleware import _safe_relative_path, _get_working_root_dir, _format_datetime, logger


def _sha256(data: bytes):
    return hashlib.sha256(data).hexdigest()


def register_read_endpoints(router: APIRouter):
    """Register all GET/read-only file endpoints on the given router."""

    @router.get("/api/tree")
    async def list_tree(path: str = Query("", description="Relative path inside working_root folder")):
        """Return a tree listing for a given path inside the working_root folder."""
        try:
            working_root_dir = _get_working_root_dir()
            if not os.path.exists(working_root_dir):
                os.makedirs(working_root_dir, exist_ok=True)
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            items = []
            try:
                entries = os.listdir(safe_path)
            except PermissionError as pe:
                # FIX (2026-08-24): previously swallowed the error and returned an EMPTY list,
                # so the UI showed "Empty folder" while files existed on disk.
                logger.warning(f"Permission denied listing {safe_path!r}: {pe}")
                raise HTTPException(status_code=500, detail=f"Permission denied: {safe_path}")
            for entry in entries:
                full = os.path.join(safe_path, entry)
                try:
                    is_dir = os.path.isdir(full)
                    stat = os.stat(full)
                except OSError as e:
                    # FIX (2026-08-24): the entry vanished between listdir() and stat()
                    # (e.g. a file deleted while this request was in flight). Skip it
                    # instead of failing the whole listing with WinError 2.
                    logger.warning(f"Skipping vanished tree entry {full!r}: {e}")
                    continue
                item = {
                    "name": entry,
                    "type": "folder" if is_dir else "file",
                    "size": stat.st_size if not is_dir else 0,
                    "modified": _format_datetime(stat.st_mtime),
                }
                if is_dir:
                    try:
                        item["items"] = []
                        for sub in os.listdir(full):
                            # FIX (2026-08-24): must join against the folder itself.
                            # The old code joined 'sub' onto safe_path, so stat() hit a
                            # non-existent sibling path and crashed ALL of /api/tree with
                            # WinError 2 -> UI showed an empty file list.
                            sub_full = os.path.join(full, sub)
                            try:
                                sub_is_dir = os.path.isdir(sub_full)
                                sub_stat = os.stat(sub_full)
                            except OSError as e:
                                logger.warning(f"Skipping vanished tree entry {sub_full!r}: {e}")
                                continue
                            item["items"].append({
                                "name": sub,
                                "type": "folder" if sub_is_dir else "file",
                                "size": sub_stat.st_size if not sub_is_dir else 0,
                                "modified": _format_datetime(sub_stat.st_mtime),
                            })
                    except PermissionError as pe:
                        logger.warning(f"Permission denied listing folder {full!r}: {pe}")
                items.append(item)
            items.sort(key=lambda x: (x["type"] != "folder", x["name"].lower()))
            parent_path = os.path.dirname(path) if path else ""
            return {"path": path, "parent_path": parent_path, "items": items}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to list tree: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/download")
    async def download_file(path: str = Query(..., description="Relative path inside working_root folder"),
                           inline: bool = Query(False, description="Return file inline instead of as attachment")):
        """Download a single file by its relative path."""
        try:
            if not path:
                raise HTTPException(status_code=400, detail="Missing path parameter")
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            if not os.path.isfile(safe_path):
                raise HTTPException(status_code=404, detail="File not found")
            filename = os.path.basename(safe_path)
            content_type, _ = mimetypes.guess_type(safe_path)
            if content_type is None:
                content_type = "application/octet-stream"
            if inline:
                return FileResponse(safe_path, media_type=content_type, headers={"Content-Disposition": f"inline; filename={filename}"})
            else:
                return FileResponse(safe_path, filename=filename, media_type=content_type)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Download error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/working-root-files")
    async def list_working_root_files():
        try:
            working_root_dir = _get_working_root_dir()
            if not os.path.exists(working_root_dir):
                os.makedirs(working_root_dir, exist_ok=True)
            files = []
            for filename in os.listdir(working_root_dir):
                filepath = os.path.join(working_root_dir, filename)
                if os.path.isfile(filepath):
                    stat = os.stat(filepath)
                    files.append({"name": filename, "size": stat.st_size, "modified": _format_datetime(stat.st_mtime)})
            files.sort(key=lambda x: x["name"].lower())
            return {"files": files}
        except Exception as e:
            logger.error(f"Failed to list working root files: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/open")
    async def open_file(filename: str):
        try:
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, filename)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path - access denied")
            if not os.path.isfile(safe_path):
                raise HTTPException(status_code=404, detail="File not found")
            # FIX (2026-08-19): was 'filepath' -- undefined NameError killed every /api/open call.
            # The resolved path variable in this function is 'safe_path' (same as /api/download).
            content_type, _ = mimetypes.guess_type(safe_path)
            if content_type is None:
                content_type = "application/octet-stream"
            return FileResponse(safe_path, media_type=content_type, filename=os.path.basename(safe_path))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Open file error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/file-content")
    async def get_file_content(path: str = Query(..., description="Relative path inside working_root folder")):
        """Get the content of a text file."""
        try:
            if not path:
                raise HTTPException(status_code=400, detail="Missing path parameter")
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            if not os.path.isfile(safe_path):
                raise HTTPException(status_code=404, detail="File not found")
            content = read_text_file(safe_path)
            return {"content": content}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get file content: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/diff")
    async def diff_files(data: Dict):
        """Compare two files and return their contents plus comparison metadata.

        FIX (2026-08-23): was GET with query params, but the UI sends POST /api/diff
        with JSON body {"path_a": ..., "path_b": ...} - FastAPI answered 405.
        REWORKED (2026-08-24): response now also carries byte-level identity and file
        metadata; the actual line diff is computed client-side by DiffCore/DiffView.
        """
        try:
            path_a = (data.get("path_a") or "").strip()
            path_b = (data.get("path_b") or "").strip()
            if not path_a or not path_b:
                raise HTTPException(status_code=400, detail="Missing path_a or path_b")
            working_root_dir = _get_working_root_dir()

            def _load(rel_path):
                safe_path = _safe_relative_path(working_root_dir, rel_path)
                if safe_path is None or not os.path.isfile(safe_path):
                    raise HTTPException(status_code=404, detail=f"File not found: {rel_path}")
                size = os.path.getsize(safe_path)
                with open(safe_path, "rb") as f:
                    raw = f.read()
                content = read_text_file(safe_path)
                return safe_path, size, raw, content

            safe_path_a, size_a, raw_a, content_a = _load(path_a)
            safe_path_b, size_b, raw_b, content_b = _load(path_b)

            # Byte-level identity (catches files that differ only in trailing newline etc.)
            identical_bytes = _sha256(raw_a) == _sha256(raw_b)

            return {
                "status": "ok",
                "file_a": {"path": path_a, "name": os.path.basename(safe_path_a), "size": size_a, "content": content_a},
                "file_b": {"path": path_b, "name": os.path.basename(safe_path_b), "size": size_b, "content": content_b},
                "identical": identical_bytes,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Diff error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/file-content")
    async def save_file_content(data: Dict):
        """Save (overwrite) the content of a text file inside working_root.

        ADDED (2026-08-24): the diff view's Save buttons POST here after the user has
        moved change blocks between files. Previously only GET existed, so saving from
        the compare view always failed with 405 Method Not Allowed.
        """
        try:
            path = (data.get("path") or "").strip()
            content = data.get("content")
            if not path:
                raise HTTPException(status_code=400, detail="Missing path")
            if not isinstance(content, str):
                raise HTTPException(status_code=400, detail="Missing content (string expected)")
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path - access denied")
            if not os.path.isfile(safe_path):
                raise HTTPException(status_code=404, detail="File not found (only existing files can be saved)")
            if not is_text_file(safe_path):
                raise HTTPException(status_code=415, detail="Only text files can be saved from the compare view")
            data_bytes = len(content.encode("utf-8"))
            if data_bytes > FILE_MAX_WRITE_BYTES:
                raise HTTPException(status_code=413, detail=f"Content too large ({data_bytes} bytes)")

            # Write exactly what the client sent. DiffCore's line model preserves a single
            # trailing newline when the original file had one (reconstruction property),
            # so no adjustment is needed here - appending another would double it up.
            with open(safe_path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            logger.info(f"Saved file content via compare view: {path} ({len(content)} chars)")
            return {"status": "saved", "path": path, "size": os.path.getsize(safe_path)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Save file content error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/working-root-info")
    async def get_working_root_info():
        """Get the current working root (single source of truth: CLIENT/config/.working_root.json)."""
        try:
            return {"working_root": get_working_root()}
        except Exception as e:
            logger.error(f"Working root info error: {e}")
            raise HTTPException(status_code=500, detail=str(e))


    @router.get("/api/file-metadata")
    async def get_file_metadata(path: str = Query(..., description="Relative path inside working_root folder")):
        """Get rich metadata for a file (size on disk, timestamps; plus image
        resolution + EXIF + GPS/Google-Maps link when the file is an image).

        ADDED (2026-09-18): powers the preview panel's image metadata card.
        Non-image files get only the plain file facts (the UI shows them too).
        """
        try:
            if not path:
                raise HTTPException(status_code=400, detail="Missing path parameter")
            working_root_dir = _get_working_root_dir()
            safe_path = _safe_relative_path(working_root_dir, path)
            if safe_path is None:
                raise HTTPException(status_code=400, detail="Invalid path")
            if not os.path.isfile(safe_path):
                raise HTTPException(status_code=404, detail="File not found")

            from app.utils.image_meta import extract_image_metadata, is_image_file

            meta = extract_image_metadata(safe_path)
            meta["path"] = path
            # Non-images: skip the (empty) image sections so the UI can render a
            # compact "file facts" card instead.
            if not is_image_file(os.path.basename(path)):
                meta.pop("image", None)
                meta.pop("exif", None)
                meta.pop("gps", None)
            return meta
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"File metadata error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/working_root")
    async def get_working_root_endpoint():
        """Get the current working root (single source of truth: CLIENT/config/.working_root.json)."""
        try:
            return {"working_root": get_working_root()}
        except Exception as e:
            logger.error(f"Working root GET error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
