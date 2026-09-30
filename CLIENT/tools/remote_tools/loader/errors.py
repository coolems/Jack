"""Loader errors -- exception types raised by the remote-tool loading pipeline."""


class LoaderError(Exception):
    """Base class for all dynamic-loader errors."""


class ToolCompilationError(RuntimeError):
    """Raised when tool source code fails to compile.

    This is a NON-RETRYABLE error -- the same source will always fail the same way.
    The retry logic in CLIENT/utils/retry.py explicitly excludes this exception
    from automatic retries (matched by class name) to prevent infinite loops on
    deterministic failures.

    Kept as a direct RuntimeError subclass for backward compatibility: callers
    (logic/react_loop.py, logic/agentic.py) catch it before generic RuntimeError.
    """


class SourceValidationError(ToolCompilationError):
    """AST security validation or syntax check rejected the delivered source."""


class IntegrityError(ToolCompilationError):
    """Fail-closed integrity failure: delivered bytes do not match the SERVER hash."""
