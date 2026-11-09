"""Read file content from working_root directory with secure path resolution."""

import os

from tools.utils import (get_working_root, resolve_path_to_dir, validate_filename,
                         guard_path_inside_working_root, locate_scratch_file)

import logging
logger = logging.getLogger(__name__)


__tool_description__ = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file's content from working_root. path = directory, filename = bare name.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Bare file name, no slashes ('config.py'). Folder goes in path."
                },
                "path": {
                    "type": "string",
                    "description": "Directory inside working_root, relative to it. '' / '.' / 'root' = working_root.",
                    "default": ""
                }
            },
            "required": ["filename"]
        }
    }
}


def read_file(filename: str, path: str = "") -> str:
    """Read a file's content from the working_root directory.

    SECURITY RULE - CRITICAL:
        * filename = JUST THE FILE NAME only (e.g. 'config.py'). NO slashes allowed!
        * path = DIRECTORY ONLY where the file lives (e.g. 'local_ai/config')
        * To read 'local_ai/CLIENT/config/config.py':  filename='config.py', path='local_ai/CLIENT/config'

    Args:
        filename: Just the file name (no path). Example: 'myfile.txt'. NEVER include '/' or '\\'.
        path: Directory path only (no filename). Relative paths resolve against working_root ONLY.
              Empty string reads from working_root. Use dot or root for working_root.

    Returns:
        The content of the file as a string, or an error message on failure.
    """
    try:
        # GUARD 1 — validate filename contains no traversal / separators
        validate_filename(filename, label="Filename")

        effective_working = get_working_root()
        dirpath = resolve_path_to_dir(effective_working, path)

        filepath = os.path.normpath(os.path.join(dirpath, filename))

        # GUARD 2 — final check: resolved absolute path must stay inside working_root
        guard_path_inside_working_root(filepath, effective_working, label="Resolved file path")

        if not os.path.isfile(filepath):
            # SCRATCH FALLBACK (2026-11-09): write_file redirects a bare plan/scratch name
            # out of the working_root top level, so a bare read of that name must find it
            # in its .temp home (.temp/plans/ for plans, .temp/ for scratch).
            scratch = locate_scratch_file(filename, effective_working)
            if scratch:
                filepath = scratch
            else:
                return f"Error: File not found: {filepath}"

        # BINARY read (2026-09-22 byte-exact fix): text mode converts CRLF -> \n and
        # would change the bytes returned to the model. Decode once at the edge;
        # strict UTF-8 keeps the original error behavior for non-UTF8 files.
        with open(filepath, 'rb') as f:
            content = f.read().decode('utf-8')

        logger.info(f"[read_file] Read '{filename}' from '{dirpath}' ({len(content)} chars)")
        return content

    except ValueError as e:
        # Path validation errors are security violations — log and reject
        logger.warning(f"[read_file] Security violation for filename='{filename}', path='{path}': {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"[read_file] Error reading '{filename}' in '{path}': {e}")
        return f"Error reading file: {e}"
