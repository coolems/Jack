"""Write file function - creates or overwrites a file."""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Create or overwrite a file. Scratch names (plan_*.md at root, tmp_/temp_/scratch_/*.tmp) are redirected into .temp/; an explicit path wins.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory; '' = working_root.",
                    "default": ""
                },
                "filename": {
                    "type": "string",
                    "description": "Bare file name, no slashes."
                },
                "content": {
                    "type": "string",
                    "description": "File content."
                },
                "mode": {
                    "type": "string",
                    "description": "'w' overwrite, 'a' append.",
                    "default": "w"
                }
            },
            "required": ["filename", "content"]
        }
    }
}

import logging
import os

from ..utils import (
    MAX_WRITE_SIZE,
    get_working_root,
    resolve_path_to_dir,
    validate_filename,
    guard_path_inside_working_root,
    normalize_line_endings,
    line_ending_name,
    is_root_level_path,
    scratch_dir_for,
)

logger = logging.getLogger("COOLEMS.Tools.Files")


def write_file(filename: str, content: str, path: str = "", mode: str = "w") -> str:
    """Create or overwrite a file with the given content.
    SECURITY RULE - CRITICAL:
        * filename = JUST THE FILE NAME only (e.g. 'config.py'). NO slashes allowed!
        * path = DIRECTORY ONLY where the file should be written (e.g. 'local_ai/config')
        * To write 'local_ai/CLIENT/client_config/settings.py':  filename='settings.py', path='local_ai/CLIENT/client_config'
    All paths are locked inside working_root. Path traversal via '..' is blocked.
              Filenames with embedded separators or traversal sequences are rejected.
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

        # SCRATCH REDIRECT (2026-11-09, user request): a scratch file must never land in
        # the working_root TOP level. Only a root-level write is redirected, and only when
        # that root file does not already exist (an existing root file keeps its own
        # history). An explicit path always wins. normcase(realpath()) so a case-different
        # or symlinked working_root still counts as the root.
        redirected_from = None
        if is_root_level_path(path):
            root_real = os.path.normcase(os.path.realpath(effective_working))
            dir_real = os.path.normcase(os.path.realpath(dirpath))
            if dir_real == root_real and not os.path.isfile(filepath):
                scratch_home = scratch_dir_for(filename, effective_working)
                if scratch_home:
                    candidate = os.path.normpath(os.path.join(scratch_home, filename))
                    # GUARD 2 applies to the redirected path too - it must stay inside
                    # working_root (realpath resolves symlinks/.. before the check).
                    guard_path_inside_working_root(candidate, effective_working,
                                                   label="Redirected scratch path")
                    redirected_from = filepath
                    filepath = candidate
                    dirpath = scratch_home

        # GUARD 2 — final check: resolved absolute path must stay inside working_root
        guard_path_inside_working_root(filepath, effective_working, label="Resolved file path")

        # Check size
        if len(content) > MAX_WRITE_SIZE:
            return "Error: Content too large"

        # Ensure directory exists
        dirname = os.path.dirname(filepath)
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        # Write file (in place - git is the backup, no side-car copy)
        # BINARY write (2026-09-22 byte-exact fix): text-mode open() on Windows
        # translates \n to CRLF and corrupts content that already carries CR/LF bytes.
        # Content was normalized to the repository line-ending policy above.
        _wmode = "wb" if mode == "w" else "ab"
        with open(filepath, _wmode) as f:
            f.write(content.encode("utf-8"))

        size = os.path.getsize(filepath)
        note = ""
        if redirected_from:
            note = (f" [scratch policy: moved out of the working_root top level - "
                    f"requested '{redirected_from}', saved as '{filepath}']")
        return (f"Success: File saved to {filepath} ({size} bytes, "
                f"{line_ending_name(filename)} line endings){note}")

    except ValueError as e:
        logger.debug("Caught expected value/key error")
        return f"Error: {e}"
    except Exception as e:
        logger.info(f"Save exception {str(e)}")
        return f"Error: {str(e)}"
