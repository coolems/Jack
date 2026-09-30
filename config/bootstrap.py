"""SERVER config bootstrap - runtime auto-creation of required config files.

CONTRACT (2026-08-23):
  * The SERVER must NEVER crash at startup because a config data file is missing.
    If a required file does not exist, it is created FROM ITS EXAMPLE TEMPLATE
    (``<name>.example.<ext>`` shipped next to the real file) so the user always
    sees exactly what shape the file has and where to put their own values.

  * Files managed here:
      - ``config/.api_keys.json``   <- seeded from ``config/.api_keys.example.json``
      - ``config/profiles.json``    <- seeded from ``config/profiles.example.json``
      - ``config/llama_servers.json``   <- seeded from ``config/llama_servers.example.json``
        (hand-editable list of llama server instances the SERVER creates at boot;
        see config.get_llama_server_instances(); per-machine, gitignored)

  * Existing files are NEVER touched (no overwrite, no migration). A missing
    file is created; a present-but-corrupt file is left alone and reported by
    the loader with an actionable error.

Call :func:`ensure_server_config_files` once at startup (code.py does this
before provider initialization). It also exists for tests: pass ``config_dir``
to operate on any directory instead of the real one.
"""

import json
import logging
import os

logger = logging.getLogger("COOLEMS.ConfigBootstrap")

# This module lives in <root>/config/ - same dir as the data files it manages.
_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))


def _seed_from_example(config_dir: str, filename: str, example_name: str) -> bool:
    """Create *filename* inside *config_dir* from its example template.

    Returns True when a new file was created, False otherwise (file already
    exists or no example available). Never overwrites an existing file.
    Falls back to a minimal valid default when the example is missing/corrupt
    so startup can never fail on this path.
    """
    target = os.path.join(config_dir, filename)
    if os.path.exists(target):
        return False

    content = None
    example_path = os.path.join(config_dir, example_name)
    try:
        with open(example_path, "r", encoding="utf-8") as f:
            raw = f.read()
        json.loads(raw)  # validate the template itself is valid JSON
        content = raw
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "[CONFIG BOOTSTRAP] Example %s missing or invalid (%s) - using built-in minimal default for %s",
            example_name, e, filename,
        )

    if content is None:
        # Minimal valid fallback (same shape as the examples).
        if filename == ".api_keys.json":
            content = json.dumps([], indent=2) + "\n"
        elif filename == "profiles.json":
            content = json.dumps({}, indent=2) + "\n"
        elif filename == "llama_servers.json":
            from config import LLAMA_SERVER_PORT as _port
            content = json.dumps([{"name": "local", "host": "127.0.0.1", "port": _port}], indent=2) + "\n"
        else:  # pragma: no cover - defensive, unknown file type
            return False

    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(
            "[CONFIG BOOTSTRAP] Created missing %s from example template. "
            "Edit it to add your own values (it is gitignored - never commit real secrets).",
            os.path.join(config_dir, filename),
        )
        return True
    except OSError as e:
        logger.error("[CONFIG BOOTSTRAP] Could not create %s: %s", target, e)
        return False


def ensure_server_config_files(config_dir: str | None = None) -> list[str]:
    """Ensure all SERVER config data files exist; seed missing ones from examples.

    Args:
        config_dir: Directory holding the config data files. Defaults to this
            module's own directory (<root>/config/). Tests may pass a temp dir.

    Returns:
        List of filenames that were created during this call (empty = nothing to do).
    """
    if config_dir is None:
        config_dir = _CONFIG_DIR

    os.makedirs(config_dir, exist_ok=True)

    created = []
    for filename, example_name in (
        (".api_keys.json", ".api_keys.example.json"),
        ("profiles.json", "profiles.example.json"),
        # 2026-09-08 multi-chat: llama server instance list (SoT for which instances we create).
        ("llama_servers.json", "llama_servers.example.json"),
    ):
        if _seed_from_example(config_dir, filename, example_name):
            created.append(filename)

    return created


if __name__ == "__main__":
    # Manual smoke test: python config/bootstrap.py
    import sys
    logging.basicConfig(level=logging.INFO)
    made = ensure_server_config_files()
    print("Created:", made or "nothing (all files present)")
    sys.exit(0)
