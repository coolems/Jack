"""Sensitive data redaction for COOLEMS CLIENT logs and stderr.

Handles: API keys, URL-encoded values, base64 tokens, bearer tokens,
access_tokens, secrets, Authorization headers, and email addresses.

Used to filter both Python logging output and raw stderr from uvicorn/websockets.
"""

import re
from urllib.parse import unquote
import sys
import logging


def redact_sensitive_data(text):
    """Comprehensive sensitive data redaction for logs/stderr.

    Handles: standard keys, URL-encoded key/values, base64 tokens,
    bearer tokens, access_tokens, secrets, and Authorization headers.

    Args:
        text: Raw text string that may contain sensitive data

    Returns:
        str: Text with all sensitive patterns replaced by [REDACTED]
    """
    # 1. api_key with any value
    text = re.sub(r'(?i)api_key=(?:%[0-9a-fA-F]{2}|[A-Za-z0-9_\-.])+(?:={0,2})?', 'api_key=[REDACTED]', text)

    # 2. URL-encoded param names containing key=
    text = re.sub(r'(?i)(?:[a-zA-Z_\-]|%[0-9a-fA-F]{2})+key=(?:%[0-9a-fA-F]{2}|[A-Za-z0-9_\-.])+(?:={0,2})?', '[REDACTED_PARAM]=[REDACTED]', text)

    # 3. access_token= parameter (OAuth tokens)
    text = re.sub(r'(?i)access_token=[A-Za-z0-9_\-.]+(?:={0,2})?', 'access_token=[REDACTED]', text)

    # 4. token= parameter (GitHub tokens, etc.)
    text = re.sub(r'(?i)(?<!access_)token=[A-Za-z0-9_\-.]+(?:={0,2})?', 'token=[REDACTED]', text)

    # 5. secret= parameter
    text = re.sub(r'(?i)secret=[A-Za-z0-9_\-.]+(?:={0,2})?', 'secret=[REDACTED]', text)

    # 6. bearer= parameter
    text = re.sub(r'(?i)bearer=[A-Za-z0-9_\-.]+(?:={0,2})?', 'bearer=[REDACTED]', text)

    # 7. Authorization: Bearer xxx header format
    text = re.sub(r'(?i)Authorization:\s*Bearer\s+[A-Za-z0-9_\-.]+(?:={0,2})?', 'Authorization: Bearer [REDACTED]', text)

    return text


def redact_email(text):
    """Remove email=... parameters from URLs (2026-10-01 hardening).

    Percent-decodes the text FIRST so both forms are caught:
        ?email=bob@example.com   -> removed
        ?email=bob%40example.com -> removed  (the encoded form leaked to disk -
                                             found by the 2026-10-01 audit)
    Only params prefixed with 'email=' are touched, so bare addresses inside
    ordinary log text (chat echoes, docs) are intentionally left alone.

    Args:
        text: Text that may contain email parameters

    Returns:
        str: Text with email= parameters removed
    """
    return re.sub(r'(?i)email=[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+', '', unquote(text))


class SensitiveStderr:
    """Simple wrapper that filters sensitive data from all stderr output.

    Used to wrap sys.stderr before starting uvicorn, since libraries like
    websockets print directly to stderr bypassing Python logging system.
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def write(self, text):
        if isinstance(text, str):
            filtered = redact_sensitive_data(text)
            filtered = redact_email(filtered)
        else:
            filtered = text
        return self._wrapped.write(filtered)

    def flush(self):
        if hasattr(self._wrapped, "flush"):
            self._wrapped.flush()

    def isatty(self):
        if hasattr(self._wrapped, "isatty"):
            return self._wrapped.isatty()
        return False


class SensitiveLogFilter(logging.Filter):
    """Redact sensitive data and email from log records.

    Used as a logging filter for uvicorn log handlers to ensure no sensitive
    data leaks through the standard Python logging system.
    """

    def filter(self, record):
        """Redact sensitive data from a log record before any handler sees it.

        Runs BEFORE logging formats the message (lazy-logging design), so we format
        early ourselves: a secret passed as a bare value -- logger.info("key=%s", KEY)
-- is invisible in record.msg and unmatchable inside record.args (no param name to
anchor on). Formatting first guarantees that whatever ends up on disk is exactly what
gets redacted. Falls back to per-field redaction if formatting fails (weird args),
which still covers the common inline-URL case.
        """
        msg = getattr(record, 'msg', None)
        if isinstance(msg, str):
            try:
                formatted = record.getMessage()
            except Exception:
                formatted = None
            if isinstance(formatted, str):
                redacted = redact_email(redact_sensitive_data(formatted))
                if redacted != formatted:
                    record.msg = redacted
                    record.args = None  # baked into msg; Formatter uses it directly
            return True
        # Fallback: redact the raw fields (pre-format shapes)
        args = getattr(record, "args", None)
        if isinstance(args, str):
            record.args = redact_email(redact_sensitive_data(args))
        elif isinstance(args, (tuple, list)):
            record.args = tuple(
                redact_email(redact_sensitive_data(a)) if isinstance(a, str) else a
                for a in args
            )
        return True


def create_uvicorn_log_config():
    """Create uvicorn logging configuration with sensitive data filtering.

    Returns:
        dict: Logging config dictionary for uvicorn.Config()
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "redact_sensitive": {
                "()": SensitiveLogFilter,
            },
        },
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "filters": ["redact_sensitive"],
            },
        },
        "loggers": {
            # level 100 = off by default; if access logging is ever re-enabled, lines flow through
            # the redacting "default" handler instead of bypassing it (2026-10-01).
            "uvicorn.access": {"level": 100, "handlers": ["default"], "propagate": False}
        },
    }


def wrap_stderr():
    """Replace sys.stderr with a SensitiveStderr wrapper.

    Returns:
        tuple: (original_stderr, wrapped_stderr) for potential restoration
    """
    original_stderr = sys.stderr
    sys.stderr = SensitiveStderr(original_stderr)
    return original_stderr, sys.stderr


def restore_stderr(original_stderr):
    """Restore the original stderr after uvicorn shutdown.

    Args:
        original_stderr: The original sys.stderr to restore
    """
    sys.stderr = original_stderr
