"""
Retry logic for provider errors.

Provides automatic retry with exponential backoff for transient errors
when the AI provider (llama.cpp, Ollama) is down or not responding.

MAX_RETRIES: 60 (5 minutes at 5-second intervals)
RETRY_DELAY: 5 seconds between attempts

AUTO-RESTART:
    If the provider is detected as down (connection/read error), the system
    attempts to restart the provider server IMMEDIATELY on the first failure.
    Subsequent retries then proceed against the restarted server.

SPECIAL HANDLING for coolems_client (SERVER connection):
    - Cannot auto-restart a remote SERVER
    - Sends ONE clean warning message, then retries silently in background
    - No per-attempt spam — just "Server unavailable" once

RECOVERY WARNINGS:
    When errors occur, the system sends recovery_warning messages to the UI.
    These create visible orange warning blocks in the chat explaining what happened.

CANCELLATION HANDLING:
    UserCancelled exceptions (from clicking stop button) are handled gracefully —
    logged at INFO level with NO error traceback or ugly logs.
    
    Also recognizes WebSocketDisconnect from FastAPI as cancellation when 
    stop_event is set, since client disconnection IS the user stopping generation.

STALE RESPONSE PREVENTION:
    After a successful provider call returns, we check if stop_event was set during
    execution. If yes → raise UserCancelled instead of returning stale result.
    This prevents old responses from bubbling up as valid after user clicked stop.

NON-RETRYABLE ERRORS:
    ToolCompilationError (from dynamic_loader) is explicitly excluded from retry.
    These are deterministic failures — the same source code will always fail to compile
    the same way, so retrying is pointless and wastes time.
"""

import asyncio
import logging
from typing import Any, Callable, Awaitable, Optional, Tuple

logger = logging.getLogger("COOLEMS.Retry")

# Retry configuration
MAX_RETRIES = 60  # 5 minutes / 5 seconds = 60 attempts
RETRY_DELAY = 5  # seconds between retries

# Import httpx errors for proper detection
try:
    import httpx
    HTTPX_ERRORS = (
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.CloseError,
        httpx.NetworkError,
        httpx.TransportError,
    )
except ImportError:
    HTTPX_ERRORS = ()

# Errors that should trigger a retry
RETRYABLE_ERRORS = (
    ConnectionError,
    TimeoutError,
    FileNotFoundError,
    OSError,
) + HTTPX_ERRORS


def asyncio_json_dumps(data: dict) -> str:
    """Helper to create JSON string for WebSocket messages."""
    import json
    return json.dumps(data)


async def _send_recovery_message(recovery_callback, websocket, message, status="warning"):
    """Send a recovery message via callback or fall back to system message."""
    if recovery_callback:
        try:
            await recovery_callback(websocket, message, status)
        except Exception:
            pass  # Fallback to system message below
    # Always also send as system for notification
    try:
        await websocket.send_text(asyncio_json_dumps({
            "type": "system",
            "content": message
        }))
    except Exception:
        logger.debug("Send operation failed silently at CLIENT/utils/retry.py:78")


def _is_user_cancelled(error: BaseException, stop_event=None) -> bool:
    """Check if an exception is a user-initiated cancellation (not a real error).
    
    Recognizes multiple cancellation indicators:
    - UserCancelled class name (from our provider modules)
    - WebSocketDisconnect from FastAPI (client disconnected = user stopped)
    - Any exception with 'cancel' in the module path or class name
    
    When stop_event is provided and set, treats connection-related exceptions as cancellations.
    """
    error_class_name = type(error).__name__
    
    # Direct match for our UserCancelled exception
    if error_class_name == "UserCancelled":
        return True
    
    # WebSocketDisconnect from FastAPI — this IS user cancellation when stop_event is set
    if error_class_name == "WebSocketDisconnect":
        if stop_event and stop_event.is_set():
            return True
        # Also treat it as cancellation even without explicit stop_event check,
        # since client disconnection during generation = user stopped
        return True
    
    # Check module path for coolems + cancel pattern
    error_module = type(error).__module__
    if "coolems" in error_module and "cancel" in error_class_name.lower():
        return True
    
    # Generic connection closed exceptions when stop is active
    if stop_event and stop_event.is_set() and error_class_name in (
        "ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError"
    ):
        return True
    
    return False


def _is_non_retryable_error(error: BaseException) -> bool:
    """Check if an exception should NOT be retried.

    ToolCompilationError is a deterministic failure — the same source code will always
    fail to compile the same way, so retrying wastes time and produces identical errors.

    Args:
        error: The exception to check.

    Returns:
        True if the error should NOT be retried.
    """
    # Check for ToolCompilationError by name (lazy import to avoid circular dependency)
    error_class_name = type(error).__name__
    
    # ToolCompilationError from dynamic_loader — deterministic, never retry
    if error_class_name == "ToolCompilationError":
        return True
    
    # Any RuntimeError that contains 'compilation' or 'compile' in the message
    # These are also deterministic tool compilation failures
    if isinstance(error, RuntimeError) and ('compilation' in str(error).lower() or 'compile' in str(error).lower()):
        return True
    
    return False


class UserCancelled(Exception):
    """Raised when user cancelled generation — NOT an error, just clean cancellation."""
    pass


async def retry_with_backoff(
    func: Callable[..., Awaitable],
    websocket: Any,
    stop_event: Optional[asyncio.Event],
    provider_name: str = "provider",
    max_retries: int = MAX_RETRIES,
    retry_delay: float = RETRY_DELAY,
    auto_restart_provider: Optional[Any] = None,
    recovery_callback: Optional[Callable] = None,
    **kwargs,
) -> Tuple[Any, bool]:
    """
    Retry a provider call with backoff when transient errors occur.

    For coolems_client (SERVER connection):
        - Sends ONE clean "Server unavailable" warning to UI on first failure
        - Retries silently in background — no per-attempt spam
        - Cannot auto-restart remote SERVER, so just keeps trying
        - Final message only when all retries exhausted

    For local providers (llama, ollama):
        - Full retry logging with per-attempt details
        - Auto-restarts the provider on first connection failure
        - Sends detailed recovery messages to UI

    CANCELLATION:
        UserCancelled exceptions are handled gracefully — logged at INFO level
        and returned immediately without retries or error spam.
        
        WebSocketDisconnect from FastAPI is also treated as cancellation when 
        stop_event is set (client disconnected = user stopped generation).

    STALE RESPONSE PREVENTION:
        After a successful provider call returns, we check if stop_event was set during
        execution. If yes → raise UserCancelled instead of returning stale result.
        This prevents old responses from bubbling up as valid after cancellation.

    NON-RETRYABLE ERRORS:
        ToolCompilationError and similar deterministic failures are returned immediately
        without any retry attempt, since the same source will always fail identically.
    """
    last_error = None
    start_time = asyncio.get_event_loop().time()
    restart_attempted = False
    is_coolems_client = (provider_name == "coolems_client")

    for attempt in range(1, max_retries + 1):
        # Check if user wants to stop (sent new message or disconnected)
        if stop_event and stop_event.is_set():
            logger.info(f"[RETRY] User cancelled - stopping retries after {attempt - 1} attempt(s)")
            return last_error or Exception("User cancelled"), False

        try:
            # Only log first attempt for coolems_client to avoid spam
            if not is_coolems_client or attempt == 1:
                logger.info(f"[RETRY] Attempt {attempt}/{max_retries} calling {provider_name}...")
            result = await func(**kwargs)

            # ============================================================
            # STALE RESPONSE PREVENTION — check if stop_event was set DURING execution.
            # If yes, the user clicked stop or sent a new message while we were generating.
            # Discard the stale result and raise UserCancelled instead of returning it.
            # This prevents old responses from bubbling up as valid after cancellation.
            # ============================================================
            if stop_event and stop_event.is_set():
                logger.info(f"[RETRY] Provider returned but user cancelled during generation — discarding stale response")
                raise UserCancelled("User cancelled generation.")

            # Success!
            total_time = asyncio.get_event_loop().time() - start_time
            if attempt > 1:
                logger.info(
                    f"[RETRY] Success on attempt {attempt} "
                    f"(took {total_time:.1f}s total)"
                )
            return result, True

        except UserCancelled as e:
            # Clean cancellation — no retries needed
            logger.info(f"[RETRY] User cancelled generation (conv via {provider_name})")
            return e, False

        except RETRYABLE_ERRORS as e:
            # Check if this is actually a user cancellation disguised as ConnectionError
            if _is_user_cancelled(e, stop_event):
                logger.info(f"[RETRY] User cancelled generation (conv via {provider_name})")
                return e, False

            last_error = e
            elapsed = asyncio.get_event_loop().time() - start_time
            remaining_attempts = max_retries - attempt
            remaining_time = remaining_attempts * retry_delay

            # ---- Logging (suppressed for coolems_client after first warning) ----
            if is_coolems_client:
                logger.debug(
                    f"[RETRY] Attempt {attempt}/{max_retries} failed: {type(e).__name__}"
                )
            else:
                logger.warning(
                    f"[RETRY] Attempt {attempt}/{max_retries} failed: {type(e).__name__}: {e}\n"
                    f"[RETRY] Elapsed: {elapsed:.1f}s, Remaining: ~{remaining_time:.0f}s"
                )

            # ---- AUTO-RESTART or SILENT RETRY for coolems_client ----
            if not restart_attempted and auto_restart_provider and hasattr(auto_restart_provider, 'start_server'):
                error_name = type(e).__name__
                connection_errors = ("ConnectionError", "ConnectError", "ReadError", "NetworkError", "TransportError")

                if error_name in connection_errors:
                    restart_attempted = True

                    # ===== coolems_client: Cannot restart remote SERVER — go silent =====
                    if is_coolems_client:
                        logger.info("[RETRY] SERVER unavailable — retrying silently in background...")
                        next_delay = retry_delay

                        # Send ONE clean warning on first failure, then stay quiet
                        if attempt == 1:
                            await _send_recovery_message(
                                recovery_callback, websocket,
                                "Server connection lost. Retrying automatically...",
                                "warning"
                            )
                    else:
                        # ===== Local provider: Attempt auto-restart =====
                        logger.warning(f"[AUTO-RESTART] {error_name} detected - attempting to restart {provider_name}...")

                        await _send_recovery_message(
                            recovery_callback, websocket,
                            f"\u26a0\ufe0f {provider_name} is not running. Attempting automatic restart...",
                            "warning"
                        )

                        try:
                            restarted = await asyncio.to_thread(auto_restart_provider.start_server)
                            if restarted:
                                logger.info(f"[AUTO-RESTART] {provider_name} restarted successfully!")
                                await _send_recovery_message(
                                    recovery_callback, websocket,
                                    f"\u2705 {provider_name} restarted. Retrying your request...",
                                    "success"
                                )
                                next_delay = 10  # Give server a moment to stabilize
                            else:
                                logger.error(f"[AUTO-RESTART] Failed to restart {provider_name}")
                                await _send_recovery_message(
                                    recovery_callback, websocket,
                                    f"\u274c Could not restart {provider_name}. Will keep retrying (up to {max_retries} attempts)...",
                                    "error"
                                )
                                next_delay = retry_delay
                        except Exception as restart_err:
                            logger.error(f"[AUTO-RESTART] Error during restart attempt: {restart_err}")
                            await _send_recovery_message(
                                recovery_callback, websocket,
                                f"\u274c Restart failed: {restart_err}. Will keep retrying...",
                                "error"
                            )
                            next_delay = retry_delay
                else:
                    # Non-connection error - just notify and retry normally (skip for coolems_client)
                    next_delay = retry_delay
                    if not is_coolems_client and attempt == 1:
                        await _send_recovery_message(
                            recovery_callback, websocket,
                            f"\u26a0\ufe0f {provider_name} error: {error_name}. Retrying automatically (up to {max_retries} attempts, {retry_delay}s apart)...",
                            "warning"
                        )
            else:
                # No auto-restart available or already attempted
                next_delay = retry_delay

                if is_coolems_client:
                    # Silent for coolems_client — already warned on first failure
                    pass
                elif attempt == 1:
                    await _send_recovery_message(
                        recovery_callback, websocket,
                        f"\u26a0\ufe0f {provider_name} is not responding. Retrying automatically (up to {max_retries} attempts, {retry_delay}s apart)...",
                        "warning"
                    )
                else:
                    await _send_recovery_message(
                        recovery_callback, websocket,
                        f"\u23f3 Retry {attempt}/{max_retries} failed. Next attempt in {next_delay}s... ({remaining_time // 60:.0f}m {remaining_time % 60:.0f}s remaining)",
                        "warning"
                    )

            # Wait before next retry (but check for cancellation)
            try:
                await asyncio.wait_for(
                    _wait_with_cancellation(next_delay, stop_event),
                    timeout=next_delay + 1,
                )
            except asyncio.TimeoutError:
                pass  # Normal timeout, proceed to next attempt

        except Exception as e:
            # Check if this is a user cancellation — handle gracefully
            # WebSocketDisconnect from FastAPI bubbles up here, not in RETRYABLE_ERRORS
            if _is_user_cancelled(e, stop_event):
                logger.info(f"[RETRY] User cancelled generation (conv via {provider_name})")
                return e, False

            # Check if this is a non-retryable error (e.g., ToolCompilationError)
            if _is_non_retryable_error(e):
                logger.error(
                    f"[RETRY] Non-retryable error detected — returning immediately: "
                    f"{type(e).__name__}: {e}"
                )
                return e, False

            # Real non-retryable error (not in RETRYABLE_ERRORS and not cancellation)
            logger.error(f"[RETRY] Non-retryable error: {type(e).__name__}: {e}")
            return e, False

    # All retries exhausted
    total_time = asyncio.get_event_loop().time() - start_time

    if is_coolems_client:
        logger.info(f"[RETRY] SERVER unreachable after {max_retries} attempts — giving up")
    else:
        logger.error(
            f"[RETRY] All {max_retries} attempts failed after {total_time:.1f}s. "
            f"Last error: {last_error}"
        )

    # Notify user of final failure (clean message for coolems_client)
    try:
        if is_coolems_client:
            await _send_recovery_message(
                recovery_callback, websocket,
                "\u274c SERVER connection failed. Please ensure SERVER is running and try again.",
                "error"
            )
        else:
            await _send_recovery_message(
                recovery_callback, websocket,
                f"\u274c {provider_name} retry failed after {max_retries} attempts. Please check that {provider_name} is running and try again.",
                "error"
            )
    except Exception:
        pass  # WebSocket likely disconnected

    return last_error, False


async def _wait_with_cancellation(delay: float, stop_event: Optional[asyncio.Event]):
    """Wait for delay seconds, but return early if stop_event is set."""
    wait_task = asyncio.create_task(asyncio.sleep(delay))
    if stop_event:
        cancel_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            [wait_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
    else:
        await wait_task
