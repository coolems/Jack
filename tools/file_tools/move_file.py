"""Move file function - moves a file to another directory"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "move_file",
        "description": "Move a file or a folder (a folder moves with everything inside it), optionally renaming it.",
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Source directory; '' = working_root.",
                    "default": ""
                },
                "source_filename": {
                    "type": "string",
                    "description": "Bare file or folder name, no slashes."
                },
                "destination_path": {
                    "type": "string",
                    "description": "Destination directory; '' = working_root.",
                    "default": ""
                },
                "new_filename": {
                    "type": "string",
                    "description": "New bare name; keeps original if omitted."
                }
            },
            "required": ["source_filename", "destination_path"]
        }
    }
}

import logging
import os
import shutil

from ..utils import (get_working_root, resolve_path_to_dir, validate_filename,
                     guard_path_inside_working_root, locate_scratch_file)

logger = logging.getLogger("COOLEMS.Tools.Files")


def move_file(source_filename: str, destination_path: str, source_path: str = "", new_filename: str = None) -> str:
    """Move a file or a FOLDER to another directory (a folder goes with its whole subtree).
    SECURITY RULE - CRITICAL:
        * source_filename = JUST THE FILE OR FOLDER NAME only (e.g. 'myfile.txt'). NO slashes allowed!
        * source_path = SOURCE DIRECTORY ONLY (e.g. 'local_ai/config')
        * destination_path = DESTINATION DIRECTORY ONLY (e.g. 'local_ai/CLIENT/data')
        * new_filename = NEW NAME ONLY if renaming during the move (e.g. 'newfile.txt')
    All paths are locked inside working_root. Path traversal via '..' is blocked.
              Filenames with embedded separators or traversal sequences are rejected. BOTH
              source and destination are validated independently.
    A folder is never moved into itself or into one of its own subfolders.
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
            # SCRATCH FALLBACK (2026-11-09): a bare plan/scratch name lives in its .temp
            # home (write_file redirects it out of the working_root top level).
            scratch = locate_scratch_file(source_filename, effective_working)
            if scratch:
                source_full = scratch
            else:
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

        # GUARD 6 — a FOLDER can never be moved into itself or into its own subtree:
        # the move would carry the destination along with the source and destroy both.
        if os.path.isdir(source_full):
            source_real = os.path.normcase(os.path.realpath(source_full))
            dest_real = os.path.normcase(os.path.realpath(dest_dir))
            if dest_real == source_real or dest_real.startswith(source_real + os.sep):
                return "Error: Cannot move a folder into itself"

        # Check if destination already exists
        if os.path.exists(dest_full):
            return f"Error: Cannot move: file already exists at '{dest_full}'"

        # Move the file or the whole folder (shutil.move handles both; across drives a
        # folder degrades to copy + delete instead of failing).
        shutil.move(source_full, dest_full)

        logger.info(f"[MOVE] Successfully moved to '{dest_full}'")
        return f"Success: Moved from '{source_full}' to '{dest_full}'"

    except ValueError as e:
        logger.debug("Caught expected value/key error")
        return str(e)
    except Exception as e:
        logger.error(f"[MOVE] Failed: {e}")
        return f"Error: Move failed: {str(e)}"
