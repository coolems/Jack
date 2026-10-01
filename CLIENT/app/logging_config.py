"""
Logging configuration for COOLEMS CLIENT.

Provides:
- Rotating file handlers (10 MB max, 3 backups) to prevent log bloat
- Colored console output for development
- Separate error-only log file
- Default INFO level to suppress debug spam in production
- Verbose logging toggle via utils.logging_utils.set_verbose_logging()

Log Files:
    logs/coolems.log  - All levels (DEBUG+ when verbose, INFO+ by default)
    logs/errors.log   - ERROR+ only, always active

Usage:
    # At startup:
    from app.logging_config import setup_logging
    logger, ws_logger, provider_logger, agent_logger, db_logger = setup_logging()

    # To enable verbose logging at runtime:
    from utils.logging_utils import set_verbose_logging
    set_verbose_logging(True)
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
        """
        Format a log record with appropriate color/prefix.

        Args:
            record: The log record to format.

        Returns:
            Formatted string with color or prefix applied.
        """
        if sys.platform == "win32":
            if record.levelno >= logging.ERROR:
                record.msg = f"[ERROR] {record.msg}"
            elif record.levelno >= logging.WARNING:
                record.msg = f"[WARN] {record.msg}"
            elif record.levelno >= logging.INFO:
                record.msg = f"[INFO] {record.msg}"
            elif record.levelno >= logging.DEBUG:
                record.msg = f"[DEBUG] {record.msg}"
        else:
            if record.levelno >= logging.ERROR:
                record.msg = f"{Colors.RED}{record.msg}{Colors.RESET}"
            elif record.levelno >= logging.WARNING:
                record.msg = f"{Colors.YELLOW}{record.msg}{Colors.RESET}"
            elif record.levelno >= logging.INFO:
                record.msg = f"{Colors.GREEN}{record.msg}{Colors.RESET}"
            elif record.levelno >= logging.DEBUG:
                record.msg = f"{Colors.BLUE}{record.msg}{Colors.RESET}"
        return super().format(record)


# FIX (2026-08-17): anchor the log dir to this module's location (app/ -> <CLIENT>/logs)
# instead of CWD-relative "logs" -- a process started from another folder would
# otherwise create stray logs/ directories outside the project tree.
_LOGS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))

# Log rotation settings
MAX_LOG_BYTES: int = 10 * 1024 * 1024  # 10 MB per file
BACKUP_COUNT: int = 3                   # Keep 3 rotated backups (~40 MB total max)


def _errors_filter(record: logging.LogRecord) -> bool:
    """
    Filter that returns True only for ERROR and CRITICAL records.

    Used by the errors.log handler to only capture serious issues.

    Args:
        record: The log record to evaluate.

    Returns:
        True if the record is ERROR or CRITICAL level.
    """
    return record.levelno >= logging.ERROR


def setup_logging() -> tuple[logging.Logger, ...]:
    """
    Configure logging for COOLEMS CLIENT.

    Sets up:
    - Rotating file handler for all logs (10 MB max, 3 backups)
    - Rotating file handler for errors only
    - Colored console handler for main logger
    - Default INFO level (DEBUG suppressed unless verbose enabled)

    IMPORTANT: The COOLEMS.Agent logger (used by agentic.py) defaults to
    INFO level. To enable verbose debug logging for the agentic loop,
    call set_verbose_logging(True) from utils.logging_utils.

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
    # --- Sensitive-data redaction on FILE + CONSOLE handlers (2026-10-01 hardening) ---
    # uvicorn.error / websockets records PROPAGATE up to the root logger and were written
    # RAW into coolems.log/errors.log -- including ?api_key=... query params in WS connection
    # lines. The stderr wrapper (entry.sensitive_filter.SensitiveStderr) and
    # create_uvicorn_log_config() only covered console output, so raw keys accumulated on disk
    # (~65 occurrences found by the 2026-10-01 audit). Lazy import keeps app/ free of a
    # module-level dependency on entry/ (code_client.py is where both packages are wired).
    from entry.sensitive_filter import SensitiveLogFilter

    redact = SensitiveLogFilter()
    coolems_handler.addFilter(redact)
    errors_handler.addFilter(redact)

    # Configure root logger with ONLY file handlers (no StreamHandler
    # here to avoid duplicate console output)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Remove any existing handlers (idempotent setup)
    root.handlers.clear()
    root.addHandler(coolems_handler)
    root.addHandler(errors_handler)

    # --- Per-module loggers with DEFAULT INFO level ---
    # DEBUG output is suppressed by default to prevent log bloat.
    # Use utils.logging_utils.set_verbose_logging(True) to enable.
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
    # Same redaction on the console stream (2026-10-01): COOLEMS.* loggers write here
    # directly and bypass the uvicorn stderr wrapper.
    console_handler.addFilter(redact)
    logger.addHandler(console_handler)

    return logger, ws_logger, provider_logger, agent_logger, db_logger
