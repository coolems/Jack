"""
Centralized logging utilities for COOLEMS.

Provides:
- Verbose logger factory with runtime toggle
- Consistent log level management across modules
- Prevents debug spam from filling production logs

Usage:
    from utils.logging_utils import get_verbose_logger, set_verbose_logging

    # Enable verbose logging at runtime
    set_verbose_logging(True)

    # Get a logger that respects the verbose flag
    logger = get_verbose_logger("MyModule")
    logger.debug("This only appears when verbose is enabled")
    logger.info("This always appears")
"""

import logging
from typing import Optional

# Global verbose logging flag - False by default to prevent log spam
_verbose_enabled: bool = False

# Module-level cache for loggers to avoid recreation
_logger_cache: dict[str, logging.Logger] = {}


def set_verbose_logging(enabled: bool) -> None:
    """
    Enable or disable verbose (DEBUG) logging globally.

    When enabled, all loggers created via get_verbose_logger() will
    output DEBUG-level messages. When disabled (default), DEBUG messages
    are suppressed to prevent log file bloat.

    Args:
        enabled: True to enable verbose logging, False to disable.
    """
    global _verbose_enabled
    _verbose_enabled = enabled

    # Update all cached loggers immediately
    for logger_name in _logger_cache:
        _update_logger_level(logger_name)


def is_verbose_logging_enabled() -> bool:
    """
    Check if verbose logging is currently enabled.

    Returns:
        True if verbose logging is enabled, False otherwise.
    """
    return _verbose_enabled


def get_verbose_logger(name: str) -> logging.Logger:
    """
    Get a logger that respects the global verbose logging flag.

    When verbose logging is enabled, this logger outputs DEBUG-level
    messages. When disabled, it outputs at INFO level, suppressing
    verbose debug output.

    The logger is cached to avoid recreation on subsequent calls.

    Args:
        name: Logger name (typically module name, e.g., "COOLEMS.Agent").

    Returns:
        A configured logging.Logger instance.
    """
    if name not in _logger_cache:
        logger = logging.getLogger(name)
        _logger_cache[name] = logger

    _update_logger_level(name)
    return _logger_cache[name]


def _update_logger_level(name: str) -> None:
    """
    Update the log level for a cached logger based on the verbose flag.

    Args:
        name: Logger name to update.
    """
    if name in _logger_cache:
        _logger_cache[name].setLevel(logging.DEBUG if _verbose_enabled else logging.INFO)


def get_logger(name: str, level: Optional[int] = None) -> logging.Logger:
    """
    Get a standard logger with optional explicit level.

    Unlike get_verbose_logger(), this logger does not respect the
    global verbose flag. Use this for loggers that should always
    maintain a specific level.

    Args:
        name: Logger name.
        level: Optional explicit log level. If None, uses INFO.

    Returns:
        A configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    if level is not None:
        logger.setLevel(level)
    return logger


def setup_module_logger(name: str, verbose: bool = True) -> logging.Logger:
    """
    Setup a logger for a module, optionally respecting verbose flag.

    Convenience function for module initialization.

    Args:
        name: Logger name.
        verbose: If True, the logger respects the global verbose flag.
                 If False, it always logs at DEBUG level.

    Returns:
        A configured logging.Logger instance.
    """
    if verbose:
        return get_verbose_logger(name)
    return get_logger(name, logging.DEBUG)
