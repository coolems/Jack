"""Client-side typed provider errors.

The SERVER tags a recoverable outage frame with error_type="backend_unavailable"; the
CLIENT's transport modules (direct_mode / relay_mode) run as CLIENT disk code, so the
typed exception lives here (not delivered from SERVER). It carries the same fields the
SERVER sends so the retry loop and the resume path can inspect them without any SERVER
import.
"""


class BackendUnavailableError(ConnectionError):
    """Recoverable backend outage - the engine is down/restarting and expected to return.

    Raised when a chat frame arrives with error_type == "backend_unavailable" (Strata OOM /
    503 / no-progress watchdog / expert reload). Subclasses ConnectionError so every existing
    connection-error guard keeps working; the extra fields let the resume path preserve the
    in-flight call and (optionally) offer a model switch.
    """

    def __init__(self, message: str, *, model: str = None, retry_after_sec: int = None,
                 oom: bool = False, available_models: list = None, server_down: bool = False):
        super().__init__(message)
        self.model = model
        self.retry_after_sec = retry_after_sec
        self.oom = oom
        self.available_models = list(available_models or [])
        self.server_down = server_down


class ServerUnreachableError(BackendUnavailableError):
    """The COOLEMS SERVER itself is unreachable (not the model backend).

    Raised by the CLIENT transport when no configured SERVER address answers (direct mode)
    or the web relay is down. It is recoverable too - the resume loop waits for the SERVER
    and re-sends the in-flight messages when it returns - but there is no model catalog to
    offer a switch from, so the dialog only offers keep-waiting / cancel.
    """

    def __init__(self, message: str, *, model: str = None):
        super().__init__(message, model=model, server_down=True)


def raise_for_error_frame(msg: dict) -> None:
    """Raise the right exception for a SERVER ``{'type': 'error'}`` frame.

    A recoverable outage becomes a :class:`BackendUnavailableError` so the resume path
    preserves the in-flight call and offers a model switch; anything else stays a plain
    ``RuntimeError`` as before. Both transports (direct_mode / relay_mode) call this, so the
    typed-error decision lives in one place instead of two copies.

    A frame is recoverable when ANY of these is true (three SERVER shapes all mean "wait and
    resend", never "fail the turn"):
      - ``error_type == "backend_unavailable"``  (run_chat_core's tagged outage frame)
      - ``recoverable`` truthy                    (same frame's explicit flag)
      - ``retryable`` truthy                      (queue worker watchdog / dropped-at-pickup:
        "no node serving / backend unavailable - please retry")
    """
    err_msg = msg.get("message", "Server error") if isinstance(msg, dict) else "Server error"
    if isinstance(msg, dict) and (
        msg.get("error_type") == "backend_unavailable"
        or msg.get("recoverable")
        or msg.get("retryable")
    ):
        raise BackendUnavailableError(
            err_msg,
            model=msg.get("model"),
            retry_after_sec=msg.get("retry_after", msg.get("retry_after_sec")),
            oom=bool(msg.get("oom")),
            available_models=msg.get("available_models"),
        )
    raise RuntimeError(err_msg)
