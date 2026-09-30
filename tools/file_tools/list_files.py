"""List files function - lists files in a directory"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "List files in the working_root directory or subdirectories within it. ALL paths are locked inside working_root - absolute paths outside working_root are NOT allowed. When user asks about work folder, working folder, or current folder use path empty string to list the current working_root directory. Relative paths resolve against working_root only. If a path would escape working_root the tool returns an error telling the user to change working_root first.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path ONLY (no filename). Relative paths resolve against working_root. Empty string lists working_root. Use dot or root for working_root. Example: 'local_ai/tools/file_tools' to list files in that subfolder.",
                    "default": ""
                }
            },
            "required": []
        }
    }
}

import logging
import os

from ..utils import get_working_root, resolve_path_to_dir, guard_path_inside_working_root

logger = logging.getLogger("COOLEMS.Tools.Files")


def list_files(path: str = "") -> str:
    """List files in a directory.

    SECURITY RULE - CRITICAL:
        * path = DIRECTORY ONLY (no filename). Relative paths resolve against working_root.
        * To list 'local_ai/tools/file_tools':  path='local_ai/tools/file_tools'
        * To list working_root directly:  path='' or path='.' or path='root'

    SECURITY: All paths are locked inside working_root. Path traversal via '..' is blocked.

    Args:
        path: Directory path only (no filename). Relative paths resolved against working_root.
              Empty = working_root/. Use '.' or 'root' for work folder.

    Returns:
        Formatted list of files and directories, or error message on failure.
    """
    try:
        # Get working_root from system variable
        effective_working = get_working_root()

        # Use the secure path resolver - locks everything inside working_root
        dirpath = resolve_path_to_dir(effective_working, path)

        # GUARD — final check: resolved directory must stay inside working_root
        guard_path_inside_working_root(dirpath, effective_working, label="Resolved directory path")

        items = []
        for item in os.listdir(dirpath)[:50]:
            # Skip .deleted files
            if item.endswith(".deleted"):
                continue
            full = os.path.join(dirpath, item)
            size = os.path.getsize(full)
            items.append(f"{'DIR' if os.path.isdir(full) else 'FILE'} {size:>10} {item}")
        return "\n".join(items) if items else "No files found"
    except ValueError as e:
        logger.debug("Caught expected value/key error")
        return str(e)
    except Exception as e:
        return f"Error: {str(e)}"
