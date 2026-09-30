"""Move file function - moves a file to another directory"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "move_file",
        "description": "Move a file to another directory inside the working_root. ALL paths are locked inside working_root - absolute paths outside working_root are NOT allowed. Source and destination use path (directory only, no filename) and filename (just the file name). Relative paths resolve against working_root ONLY. If path is empty, the file is in working_root folder. Use path dot or path root to access working_root directly. Paths outside working_root are NOT allowed - ask user to change working_root first.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Source directory path ONLY (no filename). Relative paths resolve against working_root. Empty string uses working_root. Use dot or root for working_root. Example: 'local_ai/config' to move a file from that subfolder.",
                    "default": ""
                },
                "source_filename": {
                    "type": "string",
                    "description": "Source FILE NAME ONLY (no path, no slashes). Example: 'myfile.txt'. NEVER include '/' or '\\' - use the 'source_path' parameter for directories."
                },
                "destination_path": {
                    "type": "string",
                    "description": "Destination directory path ONLY (no filename). Relative paths resolve against working_root. Empty string uses working_root. Use dot or root for working_root. Example: 'local_ai/CLIENT/data' to move a file into that subfolder.",
                    "default": ""
                },
                "new_filename": {
                    "type": "string",
                    "description": "Optional new FILE NAME ONLY (keeps original if not provided). Just the name, no slashes. NEVER include '/' or '\\'."
                }
            },
            "required": ["source_filename", "destination_path"]
        }
    }
}

import logging
import os
import shutil

from ..utils import get_working_root, resolve_path_to_dir, validate_filename, guard_path_inside_working_root

logger = logging.getLogger("COOLEMS.Tools.Files")


def move_file(source_filename: str, destination_path: str, source_path: str = "", new_filename: str = None) -> str:
    """
    Move a file to another directory.

    SECURITY RULE - CRITICAL:
        * source_filename = JUST THE FILE NAME only (e.g. 'myfile.txt'). NO slashes allowed!
        * source_path = SOURCE DIRECTORY ONLY (e.g. 'local_ai/config')
        * destination_path = DESTINATION DIRECTORY ONLY (e.g. 'local_ai/CLIENT/data')
        * new_filename = NEW FILE NAME ONLY if renaming during move (e.g. 'newfile.txt')

    SECURITY: All paths are locked inside working_root. Path traversal via '..' is blocked.
              Filenames with embedded separators or traversal sequences are rejected.
              BOTH source and destination are validated independently.

    Args:
        source_filename: Source file name only (no path). NEVER include '/' or '\\'.
        destination_path: Destination directory path only (no filename). Relative resolved against working_root.
                          Empty = working_root/. Use '.' or 'root' for work folder.
        source_path: Source directory path only (no filename). Relative resolved against working_root.
                     Empty = working_root/. Use '.' or 'root' for work folder.
        new_filename: Optional new filename (just the name, no slashes).

    Returns:
        Status message.
    """
    logger.info(f"[MOVE] Moving '{source_filename}' from '{source_path}' to '{destination_path}'")

    try:
        # Get working_root from system variable
        effective_working = get_working_root()

        # GUARD 1 — validate source filename contains no traversal / separators
        validate_filename(source_filename, label="Source filename")

        # Use the secure path resolver for SOURCE - locks inside working_root
        source_dir = resolve_path_to_dir(effective_working, source_path)

        source_full = os.path.normpath(os.path.join(source_dir, source_filename))

        # GUARD 2 — final check: resolved source absolute path must stay inside working_root
        guard_path_inside_working_root(source_full, effective_working, label="Resolved source file path")

        if not os.path.exists(source_full):
            return f"Error: Source file not found: {source_full}"

        # Use the secure path resolver for DESTINATION - locks inside working_root
        dest_dir = resolve_path_to_dir(effective_working, destination_path)

        # GUARD 3 — final check: resolved destination directory must stay inside working_root
        guard_path_inside_working_root(dest_dir, effective_working, label="Resolved destination directory")

        # Use new_filename if provided, otherwise keep original
        filename = new_filename if new_filename else os.path.basename(source_full)

        # GUARD 4 — validate new_filename too (if supplied by user)
        if new_filename:
            validate_filename(new_filename, label="New filename")

        dest_full = os.path.normpath(os.path.join(dest_dir, filename))

        # GUARD 5 — final check: resolved destination file path must stay inside working_root
        guard_path_inside_working_root(dest_full, effective_working, label="Resolved destination file path")

        # Check if destination already exists
        if os.path.exists(dest_full):
            return f"Error: Cannot move: file already exists at '{dest_full}'"

        # Move the file
        shutil.move(source_full, dest_full)

        logger.info(f"[MOVE] Successfully moved to '{dest_full}'")
        return f"Success: File moved from '{source_full}' to '{dest_full}'"

    except ValueError as e:
        logger.debug("Caught expected value/key error")
        return str(e)
    except Exception as e:
        logger.error(f"[MOVE] Failed: {e}")
        return f"Error: Move failed: {str(e)}"
