"""Read file content from working_root directory with secure path resolution.

Part reading (2026-11-26): read_file accepts an optional ``part`` argument so a
large file can be read in fixed-size chunks instead of one giant result that the
agent's 16K-char tool-result budget truncates (see AGENTIC_TOOL_RESULT_HISTORY_MAX_CHARS
in CLIENT/client_config/config.py). Each chunk is READ_FILE_PART_CHARS chars and the
returned text is prefixed with a ``[read_file <name> - part P/T]`` marker so the
caller always knows which slice it got and how many exist. Omitting ``part`` returns
the whole file (the original behaviour), which is fine for files that fit the budget.
"""

import math
import os

from tools.utils import (get_working_root, resolve_path_to_dir, validate_filename,
                         guard_path_inside_working_root, locate_scratch_file)

import logging
logger = logging.getLogger(__name__)


# Chars per part. Sized to fit a single read under the agent's 16K-char tool-result
# budget with room for the part marker line, so a requested part is never itself
# truncated before the model sees it. The split happens on the decoded string, so
# multi-byte UTF-8 characters are never cut in half.
READ_FILE_PART_CHARS: int = 15_000


__tool_description__ = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a file's text content. Use 'part' to read a large file in chunks: "
            "the response is prefixed with '[read_file <name> - part P/T]' so you know "
            "which slice you got and how many parts the file has in total."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Bare file name, no slashes ('config.py')."
                },
                "path": {
                    "type": "string",
                    "description": "Directory; '' = working_root.",
                    "default": ""
                },
                "part": {
                    "type": "integer",
                    "description": (
                        "1-based part number to read (omit or 0 = whole file). Each part is "
                        f"{READ_FILE_PART_CHARS} chars; the reply's 'part P/T' marker lists "
                        "the total T so you can fetch the remaining parts one by one."
                    ),
                    "default": 0
                }
            },
            "required": ["filename"]
        }
    }
}


def read_file(filename: str, path: str = "", part: int = 0) -> str:
    """Read a file's content from the working_root directory.

        SECURITY RULE - CRITICAL:
            * filename = JUST THE FILE NAME only (e.g. 'config.py'). NO slashes allowed!
            * path = DIRECTORY ONLY where the file lives (e.g. 'local_ai/config')
            * To read 'local_ai/CLIENT/client_config/config.py':  filename='config.py', path='local_ai/CLIENT/client_config'

        Args:
            filename: Just the file name (no path). Example: 'myfile.txt'. NEVER include '/' or '\\'.
            path: Directory path only (no filename). Relative paths resolve against working_root ONLY.
                  Empty string reads from working_root. Use dot or root for working_root.
            part: 1-based part number to return, or 0/omitted for the whole file. The result
                  is prefixed with ``[read_file <name> - part P/T]`` when a part is requested.

        Returns:
            The content (or the requested part) as a string, or an error message on failure.

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

        total = max(1, math.ceil(len(content) / READ_FILE_PART_CHARS))

        # Whole-file read (original behaviour): return everything unchanged.
        if part is None or part <= 0:
            logger.info(f"[read_file] Read '{filename}' from '{dirpath}' ({len(content)} chars)")
            return content

        if part > total:
            return (f"Error: part {part} requested but '{filename}' has only {total} "
                    f"part(s) ({len(content)} chars). Use part=1..{total}.")

        start = (part - 1) * READ_FILE_PART_CHARS
        end = min(start + READ_FILE_PART_CHARS, len(content))
        chunk = content[start:end]

        marker = (f"[read_file {filename} - part {part}/{total} "
                  f"(chars {start}-{end} of {len(content)})]")
        logger.info(f"[read_file] Read '{filename}' part {part}/{total} "
                    f"('{dirpath}', chars {start}-{end} of {len(content)})")
        return marker + "\n" + chunk

    except ValueError as e:
        # Path validation errors are security violations — log and reject
        logger.warning(f"[read_file] Security violation for filename='{filename}', path='{path}': {e}")
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"[read_file] Error reading '{filename}' in '{path}': {e}")
        return f"Error reading file: {e}"
