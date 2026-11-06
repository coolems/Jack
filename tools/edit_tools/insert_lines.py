"""insert_lines - add literal text after a specific line of an existing file.

The `insert` command of deepseek-harness's str_replace_editor (ported 2026-11-06 alongside
edit_file). Appending to the END of a file is write_file(mode='a'); this targets a line in the
MIDDLE, which edit_file cannot do without inventing an old_string spanning the seam.

Line numbers are 1-based, matching how read_file's content counts lines:
  after_line=0  -> text goes at the very start of the file
  after_line=N  -> text becomes line N+1, everything from N+1 shifts down
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "insert_lines",
        "description": "Insert literal text after a given 1-based line of an existing text file inside working_root. ALL paths are locked inside working_root - absolute paths outside working_root are NOT allowed. Path parameter is directory only (no filename) - relative paths resolve against working_root ONLY. Filename is just the file name, no path. If path is empty, the file is in working_root folder. after_line=0 inserts at the start of the file; after_line=N inserts after line N (the new text becomes line N+1). Use edit_file to change existing text and write_file(mode='a') to append at the end. Line endings follow the repository policy: LF for normal text, CRLF only for .bat/.cmd.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Just the FILE NAME ONLY (no path, no slashes) of the existing file. Example: 'config.py'. NEVER include '/' or '\\' - use the 'path' parameter for directories."
                },
                "after_line": {
                    "type": "integer",
                    "description": "1-based line to insert after. 0 inserts at the start of the file. Must not exceed the file's line count."
                },
                "text": {
                    "type": "string",
                    "description": "Literal text to insert. A trailing newline is kept; without one, the block is closed with a newline so it becomes a real line."
                },
                "path": {
                    "type": "string",
                    "description": "Directory path ONLY (no filename). Relative paths resolve against working_root. Empty string means working_root. Use dot or root for working_root. Example: 'tools/file_tools' to insert into a file inside that subfolder.",
                    "default": ""
                }
            },
            "required": ["filename", "after_line", "text"]
        }
    }
}

import logging
import os

from ..utils import (
    MAX_FILE_SIZE,
    MAX_WRITE_SIZE,
    get_working_root,
    guard_path_inside_working_root,
    line_ending_name,
    normalize_line_endings,
    resolve_path_to_dir,
    validate_filename,
)
from ._edit_core import changed_region, copy_backup, count_lines, insert_text

logger = logging.getLogger("COOLEMS.Tools.Edit")


def insert_lines(filename: str, after_line: int, text: str, path: str = "") -> str:
    """Insert literal text after line *after_line* of an existing file inside working_root.

    SECURITY RULE - CRITICAL:
        * filename = JUST THE FILE NAME only (e.g. 'config.py'). NO slashes allowed!
        * path = DIRECTORY ONLY where the file lives (e.g. 'tools/file_tools')

    SECURITY: All paths are locked inside working_root. Path traversal via '..' is blocked.
              Filenames with embedded separators or traversal sequences are rejected.

    Args:
        filename: Just the file name, no path. NEVER include '/' or '\\'.
        after_line: 1-based line to insert after (0 = start of file).
        text: Literal text to insert.
        path: Directory path only, resolved against working_root. Empty = working_root.

    Returns:
        Success message with the inserted position, new line count and byte size, or
        'Error: ...' carrying the reason (not found / bad line number / outside working_root).
    """
    logger.info(f"[insert_lines] {filename!r} in {path!r} after line {after_line!r}")

    try:
        if not filename or not filename.strip():
            return "Error: filename is required. Example: 'myfile.txt'"
        if not isinstance(text, str):
            return ("Error: text must be a string. Convert bytes to text with .decode('utf-8') "
                    "before calling insert_lines.")
        try:
            after_line = int(after_line)
        except (TypeError, ValueError):
            return f"Error: after_line must be an integer, got {after_line!r}."
        if after_line < 0:
            return "Error: after_line must be >= 0 (0 inserts at the start of the file)."
        if len(text) > MAX_WRITE_SIZE:
            return "Error: text too large"

        # GUARD 1 - filename carries no traversal / separators
        validate_filename(filename, label="Filename")

        effective_working = get_working_root()
        dirpath = resolve_path_to_dir(effective_working, path)
        filepath = os.path.normpath(os.path.join(dirpath, filename))

        # GUARD 2 - resolved absolute path must stay inside working_root
        guard_path_inside_working_root(filepath, effective_working, label="Resolved file path")

        if not os.path.isfile(filepath):
            return (f"Error: File not found: {filepath} - use write_file to create it, "
                    f"or list_files to confirm the path.")
        if os.path.getsize(filepath) > MAX_FILE_SIZE:
            return f"Error: File too large to edit ({os.path.getsize(filepath)} bytes)."

        with open(filepath, "rb") as f:
            raw = f.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"Error: '{filename}' is not UTF-8 text - insert_lines only patches text files."

        # Insert on the LF view, then re-apply the file's line-ending policy (see edit_file).
        view = normalize_line_endings(content, filename)
        block = normalize_line_endings(text, filename)

        total_lines = count_lines(view)
        if after_line > total_lines:
            return (f"Error: after_line={after_line} is past the end of '{filename}' "
                    f"({total_lines} lines). Use write_file(mode='a') to append, or a line "
                    f"number <= {total_lines}.")

        updated_view = insert_text(view, after_line, block)
        if updated_view == view:
            return f"Error: insertion produced no change in '{filename}'."

        updated = normalize_line_endings(updated_view, filename)

        # Backup before mutating, exactly like write_file's overwrite path.
        copy_backup(filepath)

        # BINARY write: text-mode open() on Windows would re-translate the newlines.
        with open(filepath, "wb") as f:
            f.write(updated.encode("utf-8"))

        size = os.path.getsize(filepath)
        head = (f"Success: inserted {len(block)} chars after line {after_line} in {filename} "
                f"({count_lines(updated_view)} lines now, {size} bytes, "
                f"{line_ending_name(filename)} line endings)")
        diff = changed_region(view, updated_view)
        return f"{head}\n{diff}" if diff else head

    except ValueError as e:
        logger.warning(f"[insert_lines] Security violation for filename={filename!r}, "
                       f"path={path!r}: {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"[insert_lines] Failed on {filename!r} in {path!r}: {e}")
        return f"Error: insert failed: {e}"
