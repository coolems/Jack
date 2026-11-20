"""Typed provider errors shared across the llama/strata/deepseek backend paths.

BackendUnavailableError is the recoverable case: the inference engine is DOWN or
RESTARTING (HTTP 503, OOM, "engine exited before it was ready", no-progress watchdog,
expert reload). It subclasses ConnectionError so every existing connection-error guard
keeps working, but it carries extra signal so the SERVER can tag the WebSocket error
frame and the CLIENT can preserve the in-flight call and resume it (or offer a model
switch) instead of failing the whole turn as non-retryable.
"""


class BackendUnavailableError(ConnectionError):
    """Recoverable backend outage - retryable, resume-safe.

    Distinct from a bare ConnectionError ("server not running / refused"): this one means
    the engine accepted the role but is currently down and expected to come back (Strata
    reloading its experts after an OOM). Fields:
        model:            the model id the request targeted (None if unknown).
        retry_after_sec:  optional hint (seconds) before the backend is likely ready.
        oom:              True when the engine reported running out of memory.
        reason:           the raw body/description verbatim (for logs and the dialog).
    """

    def __init__(self, message: str, *, model: str = None,
                 retry_after_sec: int = None, oom: bool = False, reason: str = ""):
        super().__init__(message)
        self.model = model
        self.retry_after_sec = retry_after_sec
        self.oom = oom
        self.reason = reason or message


class ContextWindowExceededError(RuntimeError):
    """The request prompt exceeds the engine's context window (HTTP 400 / 413).

    The engine rejects the WHOLE request BEFORE generation when the prompt alone
    (messages + tool schema) is larger than its context window ("request exceeds the
    context window ... requests are never truncated"). This is NOT an outage: waiting
    will not fix it. The caller must send less context (trim history / downscale images
    / drop tools) and re-send. Fields:
        model:          the model id the request targeted (None if unknown).
        prompt_tokens:  the token count the engine reported for the prompt.
        context_window: the window the engine enforced.
        reason:         the raw body/description verbatim.
    """

    def __init__(self, message: str, *, model: str = None,
                 prompt_tokens: int = None, context_window: int = None,
                 reason: str = ""):
        super().__init__(message)
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.context_window = context_window
        self.reason = reason or message
