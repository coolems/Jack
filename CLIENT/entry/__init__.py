"""COOLEMS CLIENT entry point modules.

Re-exports all public functions from submodules for clean imports.
"""

from .cli_parser import parse_args
from .port_manager import ensure_port_free
from .ssl_config import detect_ssl_config
from .sensitive_filter import (
    redact_sensitive_data,
    redact_email,
    SensitiveStderr,
    SensitiveLogFilter,
    create_uvicorn_log_config,
    wrap_stderr,
    restore_stderr,
)

__all__ = [
    "parse_args",
    "ensure_port_free",
    "detect_ssl_config",
    "redact_sensitive_data",
    "redact_email",
    "SensitiveStderr",
    "SensitiveLogFilter",
    "create_uvicorn_log_config",
    "wrap_stderr",
    "restore_stderr",
]
