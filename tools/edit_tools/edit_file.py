"""Edit a file by replacing literal text, keeping untouched content byte-for-byte.

Whole-file rewrites are slow, risk losing untouched content and fight the 10-12 kB
per-file rule, so this tool patches matched text instead. old_string must match exactly
once unless replace_all=True; matches are literal (no regex).
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "Replace literal text in an existing file (no regex). old_string must match exactly once unless replace_all; empty new_string deletes the match. Use write_file to create or fully rewrite a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Bare file name, no slashes ('read_file.py')."
                },
                "old_string": {
                    "type": "string",
                    "description": "Literal text to replace; exact match incl. whitespace. Add context to make it unique."
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text; '' deletes the match."
                },
                "path": {
                    "type": "string",
                    "description": "Directory; '' = working_root.",
                    "default": ""
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence.",
                    "default": False
                }
            },
            "required": ["filename", "old_string", "new_string"]
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
    locate_scratch_file,
)
from ._edit_core import changed_region, find_matches, line_number_at, splice

logger = logging.getLogger("COOLEMS.Tools.Edit")


def edit_file(filename: str, old_string: str, new_string: str,
              path: str = "", replace_all: bool = False) -> str:
    """Replace literal text in an existing file inside working_root.
    SECURITY RULE - CRITICAL:
        * filename = JUST THE FILE NAME only (e.g. 'config.py'). NO slashes allowed!
        * path = DIRECTORY ONLY where the file lives (e.g. 'tools/file_tools')
    All paths are locked inside working_root. Path traversal via '..' is blocked.
              Filenames with embedded separators or traversal sequences are rejected.
    """
    logger.info(f"[edit_file] {filename!r} in {path!r}: old={len(old_string or '')} chars, "
                f"new={len(new_string or '')} chars, replace_all={replace_all}")

    try:
        # --- argument validation before touching the disk ---
        if not filename or not filename.strip():
            return "Error: filename is required. Example: 'myfile.txt'"
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return ("Error: old_string and new_string must be strings. Convert bytes to text "
                    "with .decode('utf-8') before calling edit_file.")
        if old_string == "":
            return "Error: old_string must not be empty."
        if old_string == new_string:
            return "Error: old_string and new_string are identical - nothing to change."
        if len(new_string) > MAX_WRITE_SIZE:
            return "Error: new_string too large"

        # GUARD 1 - filename carries no traversal / separators
        validate_filename(filename, label="Filename")

        effective_working = get_working_root()
        dirpath = resolve_path_to_dir(effective_working, path)
        filepath = os.path.normpath(os.path.join(dirpath, filename))

        # GUARD 2 - resolved absolute path must stay inside working_root
        guard_path_inside_working_root(filepath, effective_working, label="Resolved file path")

        if not os.path.isfile(filepath):
            # SCRATCH FALLBACK (2026-11-09): a bare plan/scratch name lives in its .temp
            # home (write_file redirects it out of the working_root top level).
            scratch = locate_scratch_file(filename, effective_working)
            if scratch:
                filepath = scratch
            else:
                return (f"Error: File not found: {filepath} - use write_file to create it, "
                        f"or list_files to confirm the path.")
        if os.path.getsize(filepath) > MAX_FILE_SIZE:
            return f"Error: File too large to edit ({os.path.getsize(filepath)} bytes)."

        # Read byte-exactly (text mode would translate CRLF -> LF on Windows) and decode once;
        # strict UTF-8 keeps read_file's behavior for non-text files.
        with open(filepath, "rb") as f:
            raw = f.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"Error: '{filename}' is not UTF-8 text - edit_file only patches text files."

        # Edit on the LF view: one newline convention for matching, so a CRLF file can be
        # patched with an LF old_string. The file's policy is re-applied before writing.
        view = normalize_line_endings(content, filename)
        search = normalize_line_endings(old_string, filename)
        replacement = normalize_line_endings(new_string, filename)

        offsets = find_matches(view, search)
        if not offsets:
            return (f"Error: old_string not found in '{filename}'. Read the file first and copy "
                    f"the exact text (whitespace and indentation matter).")

        if len(offsets) > 1 and not replace_all:
            shown = [line_number_at(view, o) for o in offsets[:10]]
            more = "..." if len(offsets) > 10 else ""
            return (f"Error: old_string matches {len(offsets)} times in '{filename} "
                    f"(lines {', '.join(str(n) for n in shown)}{more}). Add more surrounding "
                    f"context to make it unique, or pass replace_all=true.")

        updated_view = splice(view, offsets, len(search), replacement)
        if updated_view == view:
            return f"Error: replacement produced no change in '{filename}'."

        updated = normalize_line_endings(updated_view, filename)

        # BINARY write: text-mode open() on Windows would re-translate the newlines.
        with open(filepath, "wb") as f:
            f.write(updated.encode("utf-8"))

        size = os.path.getsize(filepath)
        replaced = len(offsets) if replace_all else 1
        head = (f"Success: edited {filename} ({replaced} replacement(s) at line "
                f"{line_number_at(view, offsets[0])}, {size} bytes, "
                f"{line_ending_name(filename)} line endings)")
        diff = changed_region(view, updated_view)
        return f"{head}\n{diff}" if diff else head

    except ValueError as e:
        logger.warning(f"[edit_file] Security violation for filename={filename!r}, "
                       f"path={path!r}: {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"[edit_file] Failed on {filename!r} in {path!r}: {e}")
        return f"Error: edit failed: {e}"
