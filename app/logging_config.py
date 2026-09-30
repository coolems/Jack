"""
Logging configuration for COOLEMS SERVER.

Provides:
- Rotating file handlers (10 MB max, 3 backups) to prevent log bloat
- Colored console output for development
- Separate error-only log file
- Default INFO level to suppress debug spam in production

Log Files:
    logs/coolems.log  - All levels (DEBUG+ when verbose, INFO+ by default)
    logs/errors.log   - ERROR+ only, always active

Usage:
    # At startup:
    from app.logging_config import setup_logging
    logger, ws_logger, provider_logger, agent_logger, db_logger = setup_logging()
"""

import sys
import os
import logging
from logging.handlers import RotatingFileHandler


class Colors:
    """ANSI color codes for terminal output."""
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    RESET = "\033[0m"


class ColoredFormatter(logging.Formatter):
    """
    Formatter that adds color to log messages based on severity.

    On Windows (no ANSI support), uses text prefixes instead of colors.
    On Unix/Linux/macOS, uses ANSI color codes.
    """

    def format(self, record: logging.LogRecord) -> str:
        # 2026-08-18 FIX (double "[INFO] [INFO]" prefixes): the old code MUTATED
        # record.msg. A LogRecord is shared by every handler that sees it - when a
        # child logger had its own ColoredFormatter console handler AND propagated
        # to an ancestor with another one, the prefix was applied twice per line.
        # Wrap the FINAL formatted string instead: idempotent, no record mutation.
        formatted = super().format(record)
        if sys.platform == "win32":
            if record.levelno >= logging.ERROR:
                return f"[ERROR] {formatted}"
            elif record.levelno >= logging.WARNING:
                return f"[WARN] {formatted}"
            elif record.levelno >= logging.INFO:
                return f"[INFO] {formatted}"
            else:  # DEBUG and below
                return f"[DEBUG] {formatted}"
        else:
            if record.levelno >= logging.ERROR:
                return f"{Colors.RED}{formatted}{Colors.RESET}"
            elif record.levelno >= logging.WARNING:
                return f"{Colors.YELLOW}{formatted}{Colors.RESET}"
            elif record.levelno >= logging.INFO:
                return f"{Colors.GREEN}{formatted}{Colors.RESET}"
            else:
                return f"{Colors.BLUE}{formatted}{Colors.RESET}"


# FIX (2026-08-17): anchor the log dir to this module's location (app/ -> <root>/logs)
# instead of CWD-relative "logs" -- a process started from another folder would
# otherwise create stray logs/ directories outside the project tree.
_LOGS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))

# Log rotation settings
MAX_LOG_BYTES: int = 10 * 1024 * 1024  # 10 MB per file
BACKUP_COUNT: int = 3                   # Keep 3 rotated backups (~40 MB total max)


def _errors_filter(record: logging.LogRecord) -> bool:
    """Filter that returns True only for ERROR and CRITICAL records."""
    return record.levelno >= logging.ERROR


def setup_logging() -> tuple[logging.Logger, ...]:
    """
    Configure logging for COOLEMS SERVER.

    Sets up:
    - Rotating file handler for all logs (10 MB max, 3 backups)
    - Rotating file handler for errors only
    - Colored console handler for main logger
    - Default INFO level (DEBUG suppressed unless verbose enabled)

    Returns:
        Tuple of (main_logger, ws_logger, provider_logger, agent_logger, db_logger)
    """
    os.makedirs(_LOGS_DIR, exist_ok=True)

    # Fix Windows console encoding issue
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # --- coolems.log: ALL levels, rotating ---
    coolems_handler = RotatingFileHandler(
        os.path.join(_LOGS_DIR, "coolems.log"),
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    coolems_handler.setFormatter(logging.Formatter(log_format))
    coolems_handler.setLevel(logging.DEBUG)

    # --- errors.log: ERROR+ only, rotating ---
    errors_handler = RotatingFileHandler(
        os.path.join(_LOGS_DIR, "errors.log"),
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    errors_handler.setFormatter(logging.Formatter(log_format))
    errors_handler.setLevel(logging.ERROR)

    # Configure root logger with ONLY file handlers (no StreamHandler
    # here to avoid duplicate console output)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Remove any existing handlers (idempotent setup)
    root.handlers.clear()
    root.addHandler(coolems_handler)
    root.addHandler(errors_handler)

    # --- Per-module loggers with DEFAULT INFO level ---
    logger = logging.getLogger("COOLEMS")
    logger.setLevel(logging.INFO)

    ws_logger = logging.getLogger("COOLEMS.WebSocket")
    ws_logger.setLevel(logging.INFO)

    provider_logger = logging.getLogger("COOLEMS.Provider")
    provider_logger.setLevel(logging.INFO)

    agent_logger = logging.getLogger("COOLEMS.Agent")
    agent_logger.setLevel(logging.INFO)

    db_logger = logging.getLogger("COOLEMS.Database")
    db_logger.setLevel(logging.INFO)

    # Console handler attached only to the main COOLEMS logger
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        ColoredFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    # 2026-08-18 FIX (duplicate console lines): the old code attached a SECOND
    # ColoredFormatter StreamHandler to every COOLEMS.* child logger while those
    # children ALSO propagated their records up to the "COOLEMS" logger's own
    # console handler. Result: each line was printed twice, and because the old
    # formatter mutated record.msg, the second pass added a doubled prefix
    # ("[INFO] [INFO]"). Now there is exactly ONE console handler (on COOLEMS);
    # every child propagates to it by default (propagate=True) — no per-child
    # handlers needed. File output still flows via root's rotating handlers.
