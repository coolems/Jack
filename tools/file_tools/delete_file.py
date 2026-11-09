"""Delete file function - renames file by adding .deleted extension"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "delete_file",
        "description": "Soft-delete a file (adds .deleted extension). path = directory, filename = bare name.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory inside working_root, relative to it. '' / '.' / 'root' = working_root.",
                    "default": ""
                },
                "filename": {
                    "type": "string",
                    "description": "Bare file name, no slashes ('myfile.txt'). Folder goes in path."
                }
            },
            "required": ["filename"]
        }
    }
}

import logging
import os

from ..utils import (get_working_root, resolve_path_to_dir, validate_filename,
                     guard_path_inside_working_root, locate_scratch_file)

logger = logging.getLogger("COOLEMS.Tools.Files")


def delete_file(filename: str, path: str = "") -> str:
    """
    Soft-delete a file by adding .deleted extension.

    SECURITY RULE - CRITICAL:
        * filename = JUST THE FILE NAME only (e.g. 'myfile.txt'). NO slashes allowed!
        * path = DIRECTORY ONLY where the file lives (e.g. 'local_ai/config')
        * To delete 'local_ai/CLIENT/logs/app.log':  filename='app.log', path='local_ai/CLIENT/logs'

    SECURITY: All paths are locked inside working_root. Path traversal via '..' is blocked.
              Filenames with embedded separators or traversal sequences are rejected.

    Args:
        filename: Just the file name (no path). NEVER include '/' or '\\'.
        path: Directory path only (no filename). Relative resolved against working_root.
              Empty = working_root/. Use '.' or 'root' for work folder.

    Returns:
        Status message.
    """
    logger.info(f"[DELETE] Soft-deleting '{filename}' from '{path}'")

    try:
        # Validate filename is provided
        if not filename or filename.strip() == "":
            return "Error: filename is required. Example: 'myfile.txt'"

        # GUARD 1 — validate filename contains no traversal / separators
        validate_filename(filename, label="Filename")

        # Get working_root from system variable
        effective_working = get_working_root()

        # Use the secure path resolver - locks everything inside working_root
        dirpath = resolve_path_to_dir(effective_working, path)
        filepath = os.path.normpath(os.path.join(dirpath, filename))

        # GUARD 2 — final check: resolved absolute path must stay inside working_root
        guard_path_inside_working_root(filepath, effective_working, label="Resolved file path")

        if not os.path.exists(filepath):
            # SCRATCH FALLBACK (2026-11-09): a bare plan/scratch name lives in its .temp
            # home (write_file redirects it out of the working_root top level).
            scratch = locate_scratch_file(filename, effective_working)
            if scratch:
                filepath = scratch
            else:
                return f"Error: File not found: {filepath}"

        # Check if already has .deleted extension
        if filepath.endswith('.deleted'):
            return f"Error: File already deleted (has .deleted extension): {filename}"

        # Rename by adding .deleted extension
        new_filepath = filepath + ".deleted"
        os.rename(filepath, new_filepath)

        logger.info(f"[DELETE] Successfully soft-deleted '{filepath}' -> '{new_filepath}'")
        return f"Success: File soft-deleted to '{new_filepath}'"

    except ValueError as e:
        logger.debug("Caught expected value/key error")
        return str(e)
    except Exception as e:
        logger.error(f"[DELETE] Failed: {e}")
        return f"Error: Delete failed: {str(e)}"
