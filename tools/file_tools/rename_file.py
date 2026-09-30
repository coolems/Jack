"""Rename file function - renames a file"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "rename_file",
        "description": "Rename a file inside the working_root directory. ALL paths are locked inside working_root - absolute paths outside working_root are NOT allowed. Path parameter is directory only (no filename) - relative paths resolve against working_root ONLY. Old and new filenames are just the name, no path. If path is empty, file is in working_root folder. Use path dot or path root to access working_root directly. Paths outside working_root are NOT allowed - ask user to change working_root first.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path ONLY (no filename). Relative paths resolve against working_root. Empty string renames in working_root. Use dot or root for working_root. Example: 'local_ai/config' to rename a file inside that subfolder.",
                    "default": ""
                },
                "old_filename": {
                    "type": "string",
                    "description": "Current FILE NAME ONLY (no path, no slashes). Example: 'oldfile.txt'. NEVER include '/' or '\\' - use the 'path' parameter for directories."
                },
                "new_filename": {
                    "type": "string",
                    "description": "New FILE NAME ONLY (no path, no slashes). Example: 'newfile.txt'. NEVER include '/' or '\\' - use the 'path' parameter for directories."
                }
            },
            "required": ["old_filename", "new_filename"]
        }
    }
}

import logging
import os

from ..utils import get_working_root, resolve_path_to_dir, validate_filename, guard_path_inside_working_root

logger = logging.getLogger("COOLEMS.Tools.Files")


def rename_file(old_filename: str, new_filename: str, path: str = "") -> str:
    """
    Rename a file.

    SECURITY RULE - CRITICAL:
        * old_filename / new_filename = JUST THE FILE NAME only (e.g. 'old.py' -> 'new.py'). NO slashes allowed!
        * path = DIRECTORY ONLY where the file lives (e.g. 'local_ai/config')
        * To rename 'local_ai/CLIENT/config/old.py':  old_filename='old.py', new_filename='new.py', path='local_ai/CLIENT/config'

    SECURITY: All paths are locked inside working_root. Path traversal via '..' is blocked.
              Filenames with embedded separators or traversal sequences are rejected.

    Args:
        old_filename: Current file name only (no path). NEVER include '/' or '\\'.
        new_filename: New file name only (no path). NEVER include '/' or '\\'.
        path: Directory path only (no filename). Relative resolved against working_root.
              Empty = working_root/. Use '.' or 'root' for work folder.

    Returns:
        Status message.
    """
    logger.info(f"[RENAME] Renaming '{old_filename}' to '{new_filename}' in '{path}'")

    try:
        # GUARD 1 — validate BOTH filenames contain no traversal / separators
        validate_filename(old_filename, label="Old filename")
        validate_filename(new_filename, label="New filename")

        # Get working_root from system variable
        effective_working = get_working_root()

        # Use the secure path resolver - locks everything inside working_root
        directory = resolve_path_to_dir(effective_working, path)

        old_full = os.path.normpath(os.path.join(directory, old_filename))

        # GUARD 2 — final check: resolved old file path must stay inside working_root
        guard_path_inside_working_root(old_full, effective_working, label="Resolved old file path")

        if not os.path.exists(old_full):
            return f"Error: File not found: {old_full}"

        # Build new full path
        new_full = os.path.normpath(os.path.join(directory, new_filename))

        # GUARD 3 — final check: resolved new file path must stay inside working_root
        guard_path_inside_working_root(new_full, effective_working, label="Resolved new file path")

        # Check if new filename already exists
        if os.path.exists(new_full):
            return f"Error: Cannot rename: '{new_filename}' already exists"

        # Perform rename
        os.rename(old_full, new_full)

        logger.info(f"[RENAME] Successfully renamed to '{new_full}'")
        return f"Success: File renamed from '{old_filename}' to '{new_filename}'"

    except ValueError as e:
        logger.debug("Caught expected value/key error")
        return str(e)
    except Exception as e:
        logger.error(f"[RENAME] Failed: {e}")
        return f"Error: Rename failed: {str(e)}"
