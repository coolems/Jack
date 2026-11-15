"""CLIENT config bootstrap - runtime auto-creation of required CLIENT files.

CONTRACT (2026-08-23):
  * The CLIENT must NEVER crash at startup because a config data file is missing.
    If a required file does not exist, it is created FROM ITS EXAMPLE TEMPLATE
    (``<name>.example.<ext>`` shipped next to the real file) so the user always
    sees exactly what shape the file has and where to put their own values.

  * Files managed here:
      - ``CLIENT/config/.api_client_keys.json`` <- seeded from ``config/.api_client_keys.example.json``
        (the CLIENT's copy of its API credentials, sent TO the SERVER over WS)
      - ``CLIENT/config/settings.json``         <- seeded from ``config/settings.example.json``
        (single source of truth for server_address; _resolve_client_server_address()
        also self-heals it on first boot - bootstrap just makes the file visible early)

  * Existing files are NEVER touched (no overwrite, no migration). A missing file
    is created; a present-but-corrupt file is left alone and reported by its loader.

Call :func:`ensure_client_config_files` once at startup (code_client.py does this
before init_db). It also exists for tests: pass ``client_dir`` to operate on any
directory instead of the real one.
"""

import json
import logging
import os

logger = logging.getLogger("COOLEMS.ClientBootstrap")

# This module lives in <CLIENT>/config/ - its parent is the CLIENT root.
_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_CLIENT_DIR = os.path.dirname(_CONFIG_DIR)


def _seed_from_example(target_path: str, example_path: str) -> bool:
    """Create *target_path* from the JSON template at *example_path*.

    Returns True when a new file was created, False otherwise (file already
    exists or no example available). Never overwrites an existing file.
    Falls back to a minimal valid default when the example is missing/corrupt
    so startup can never fail on this path.
    """
    if os.path.exists(target_path):
        return False

    content = None
    try:
        with open(example_path, "r", encoding="utf-8") as f:
            raw = f.read()
        json.loads(raw)  # validate the template itself is valid JSON
        content = raw
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "[CLIENT BOOTSTRAP] Example %s missing or invalid (%s) - using built-in minimal default for %s",
            os.path.basename(example_path), e, os.path.basename(target_path),
        )

    if content is None:
        # Minimal valid fallback (same shape as the examples).
        name = os.path.basename(target_path)
        if name == ".api_client_keys.json":
            content = json.dumps([], indent=2) + "\n"
        elif name == "settings.json":
            content = json.dumps({"server_address": ""}, indent=2) + "\n"
        else:  # pragma: no cover - defensive, unknown file type
            return False

    try:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(
            "[CLIENT BOOTSTRAP] Created missing %s from example template. "
            "Edit it to add your own values (it is gitignored - never commit real secrets).",
            target_path,
        )
        return True
    except OSError as e:
        logger.error("[CLIENT BOOTSTRAP] Could not create %s: %s", target_path, e)
        return False


def ensure_client_config_files(client_dir: str | None = None) -> list[str]:
    """Ensure all CLIENT config data files exist; seed missing ones from examples.

    Args:
        client_dir: The CLIENT root directory (parent of ``config/``). Defaults to
            this module's own location (<CLIENT>/). Tests may pass a temp dir.

    Returns:
        List of filenames that were created during this call (empty = nothing to do).
    """
    if client_dir is None:
        client_dir = _CLIENT_DIR

    config_dir = os.path.join(client_dir, "config")
    os.makedirs(config_dir, exist_ok=True)

    # (target file, example template) pairs - all managed files live in config/.
    pairs = [
        (os.path.join(config_dir, ".api_client_keys.json"),
         os.path.join(config_dir, ".api_client_keys.example.json")),
        (os.path.join(config_dir, "settings.json"),
         os.path.join(config_dir, "settings.example.json")),
    ]

    created = []
    for target_path, example_path in pairs:
        if _seed_from_example(target_path, example_path):
            created.append(os.path.basename(target_path))

    return created


def ensure_database_file(db_path: str | None = None) -> bool:
    """Ensure the parent directory of the CLIENT database exists and is writable.

    The SQLite file itself is created by ``init_db()`` (sqlite3.connect creates an
    empty file, then tables are made). This helper only guarantees the directory
    exists so a fresh checkout can never fail on that path - it NEVER touches an
    existing database file (no overwrite, no migration).

    Args:
        db_path: Path of chat_history.db. Defaults to config.DB_PATH (<CLIENT>/chat_history.db).

    Returns:
        True when the directory was ready (already existed or created successfully),
        False when it could not be ensured (init_db will then report its own error).
    """
    if db_path is None:
        from config import DB_PATH
        db_path = DB_PATH
    try:
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        return True
    except OSError as e:
        logger.error("[CLIENT BOOTSTRAP] Could not ensure database directory for %s: %s", db_path, e)
        return False


if __name__ == "__main__":
    # Manual smoke test: python config/bootstrap.py
    import sys
    logging.basicConfig(level=logging.INFO)
    made = ensure_client_config_files()
    print("Created:", made or "nothing (all files present)")
    sys.exit(0)
