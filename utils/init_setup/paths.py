"""Repo-root path + CLIENT UI port reader.

This package is stdlib-only and must not import either config package (the SERVER
"config" and the CLIENT "CLIENT/config" packages would mix in one process, and init
runs before any venv exists). Values that live in a config file are therefore parsed
out of it as text instead of imported.
"""

import os
import re

ROOT: str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # <repo>/utils/init_setup/paths.py -> <repo>


def _read_client_ui_port() -> int:
    """Read CLIENT_UI_PORT from CLIENT/config/config.py as text - NO fallback value.

    This script is stdlib-only and must not import either config package (the SERVER
    "config" and the CLIENT "CLIENT/config" packages would mix in one process, and this
    runs before any venv exists). Same pattern as the CONTEXT_WINDOW_TOKENS patch below:
    parse the value out of the file. A missing constant or a non-integer value is a
    configuration error - it raises instead of guessing a port (no-fallback contract).
    """
    cfg_path = os.path.join(ROOT, "CLIENT", "config", "config.py")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise RuntimeError(f"Cannot read {cfg_path} to determine the CLIENT UI port: {e}") from e
    m = re.search(r"^CLIENT_UI_PORT\s*(?::\s*\w+\s*)?=\s*(\d+)\b", text, flags=re.MULTILINE)
    if not m:
        raise RuntimeError(
            "CLIENT_UI_PORT not found in CLIENT/config/config.py - cannot determine the UI port."
        )
    return int(m.group(1))
