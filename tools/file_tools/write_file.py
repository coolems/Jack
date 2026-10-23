"""Write file function - creates or overwrites a file."""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Create or overwrite a file with content inside the working_root directory. ALL paths are locked inside working_root - absolute paths outside working_root are NOT allowed. Path parameter is directory only (no filename) - relative paths resolve against working_root ONLY. Filename is just the file name, no path. If path is empty, file is created in working_root folder. Use path dot or path root to write to working_root directly. Paths outside working_root are NOT allowed - ask user to change working_root first. Line endings are normalized automatically: LF for normal text, CRLF only for .bat/.cmd.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path ONLY (no filename). Relative paths resolve against working_root. Empty string creates file in working_root. Use dot or root for working_root. Example: 'local_ai/config' to write a file into that subfolder.",
                    "default": ""
                },
                "filename": {
                    "type": "string",
                    "description": "Just the FILE NAME ONLY (no path, no slashes). Example: 'myfile.txt', 'data.csv'. NEVER include '/' or '\\' in filename - use the 'path' parameter for directories instead."
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file"
                },
                "mode": {
                    "type": "string",
                    "description": "Write mode: 'w' for overwrite, 'a' for append",
                    "default": "w"
                }
            },
            "required": ["filename", "content"]
        }
    }
}

import logging
import os
import shutil

from ..utils import (
    MAX_WRITE_SIZE,
    get_working_root,
    resolve_path_to_dir,
    validate_filename,
    guard_path_inside_working_root,
    normalize_line_endings,
    line_ending_name,
)

logger = logging.getLogger("COOLEMS.Tools.Files")


def write_file(filename: str, content: str, path: str = "", mode: str = "w") -> str:
    """
    Create or overwrite a file with the given content.

    SECURITY RULE - CRITICAL:
        * filename = JUST THE FILE NAME only (e.g. 'config.py'). NO slashes allowed!
        * path = DIRECTORY ONLY where the file should be written (e.g. 'local_ai/config')
        * To write 'local_ai/CLIENT/config/settings.py':  filename='settings.py', path='local_ai/CLIENT/config'

    SECURITY: All paths are locked inside working_root. Path traversal via '..' is blocked.
              Filenames with embedded separators or traversal sequences are rejected.

    LINE ENDINGS: content is normalized before writing -- LF for regular text and
              CRLF for .bat/.cmd (matching .gitattributes). "\r\r\n" corruption from
              older text-mode writes is repaired automatically.

    Args:
        filename: Just the file name, no path (e.g. 'myfile.txt'). NEVER include '/' or '\\'.
        content: File content to write.
        mode: Write mode - 'w' for overwrite, 'a' for append.
        path: Directory path only, no filename. Relative paths resolved against
              working_root. Empty string defaults to working_root/. Use '.' or 'root'.

    Returns:
        Success message with file path and size, or 'Error: {message}' on failure.
    """
    logger.info(f"write_file called: path={path}, filename={filename}, len={len(content) if isinstance(content, str) else 'n/a'}")

    try:
        # Validate filename is provided
        if not filename or filename.strip() == "":
            return "Error: filename is required. Example: 'myfile.txt'"

        # GUARD 1 — validate filename contains no traversal / separators
        validate_filename(filename, label="Filename")

        if not isinstance(content, str):
            return ("Error: content must be a string. Convert bytes to text (for "
                    "example with .decode('utf-8')) before calling write_file.")

        # Only 'w' (overwrite) and 'a' (append) are supported. Previously any other
        # value silently fell through to append mode, which corrupted files when a
        # model passed e.g. 'x' or 'wb'.
        mode = (mode or "w").strip().lower()
        if mode not in ("w", "a"):
            return "Error: mode must be 'w' (overwrite) or 'a' (append)."

        # Reject oversized input BEFORE line-ending normalization (which can grow
        # CRLF files) so a huge payload never gets regex-processed first.
        if len(content) > MAX_WRITE_SIZE:
            return "Error: Content too large"

        # LINE-ENDING POLICY (2026-10-23): normalize to LF (CRLF for .bat/.cmd)
        # BEFORE the final size check, so the limit applies to the bytes written.
        content = normalize_line_endings(content, filename)

        # Get working_root from system variable
        effective_working = get_working_root()

        # Use the secure path resolver - locks everything inside working_root
        dirpath = resolve_path_to_dir(effective_working, path)
        filepath = os.path.normpath(os.path.join(dirpath, filename))

        # GUARD 2 — final check: resolved absolute path must stay inside working_root
        guard_path_inside_working_root(filepath, effective_working, label="Resolved file path")

        # Check size
        if len(content) > MAX_WRITE_SIZE:
            return "Error: Content too large"

        # Ensure directory exists
        dirname = os.path.dirname(filepath)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        # Backup existing file if overwriting
        if mode == "w" and os.path.exists(filepath):
            shutil.copy2(filepath, filepath + ".backup")

        # Write file
        # BINARY write (2026-09-22 byte-exact fix): text-mode open() on Windows
        # translates \n to CRLF and corrupts content that already carries CR/LF bytes.
        # Content was normalized to the repository line-ending policy above.
        _wmode = "wb" if mode == "w" else "ab"
        with open(filepath, _wmode) as f:
            f.write(content.encode("utf-8"))

        size = os.path.getsize(filepath)
        return (f"Success: File saved to {filepath} ({size} bytes, "
                f"{line_ending_name(filename)} line endings)")

    except ValueError as e:
        logger.debug("Caught expected value/key error")
        return f"Error: {e}"
    except Exception as e:
        logger.info(f"Save exception {str(e)}")
        return f"Error: {str(e)}"
