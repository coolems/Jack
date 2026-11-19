"""Shared base code for normal and agentic chat modes.

BaseModeConfig (shared fields), build_messages(), strip_images_from_messages(),
attach_images_to_current_user_message(), is_vision_unavailable_error(), push_token_stats()
and the session-stats helpers.
"""

import logging
import re
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
import asyncio
import time

from client_config import MODEL_NAME, PROVIDER_DEFAULT_TEMPERATURE
from app.providers import BaseProvider


logger = logging.getLogger("COOLEMS.Logic.Base")


class SessionTimer:
    """Active-time timer for one chat turn.

    Counts only time actually spent working: waiting on a user-decision menu (python_exec
    approval, URL consent) is paused, so an approval wait adds nothing to the session time
    shown in the UI status bar.
    """

    def __init__(self):
        self._ever_started = False  # True once start() ran at least once
        self._started_at = None     # monotonic ts of the current running segment
        self._accumulated = 0.0     # seconds accumulated over finished segments
        self._running = False

    def start(self) -> None:
        """Begin counting active time (idempotent while already running)."""
        if not self._running:
            self._ever_started = True
            self._started_at = time.monotonic()
            self._running = True

    def pause(self) -> None:
        """Stop counting (e.g. a user decision menu is open). Idempotent."""
        if self._running and self._started_at is not None:
            self._accumulated += time.monotonic() - self._started_at
            self._started_at = None
            self._running = False

    def resume(self) -> None:
        """Resume counting after a pause. No-op before start()."""
        if self._ever_started and not self._running:
            self._started_at = time.monotonic()
            self._running = True

    @property
    def running(self) -> bool:
        return self._running

    def elapsed(self) -> float:
        """Total ACTIVE seconds so far (paused time excluded). Never negative."""
        total = self._accumulated
        if self._running and self._started_at is not None:
            total += time.monotonic() - self._started_at
        return max(0.0, total)

    def stop(self) -> float:
        """Finalize the timer; returns the final elapsed seconds."""
        self.pause()
        return self.elapsed()


@dataclass
class BaseModeConfig:
    """Base configuration shared by both normal and agentic chat modes"""
    model: str = MODEL_NAME
    temperature: float = PROVIDER_DEFAULT_TEMPERATURE
    # Thinking: bool (show reasoning or not) or canonical level
    # "none"/"low"/"medium"/"high". A level is forwarded to the SERVER, which translates
    # it into the serving model's own vocabulary (app/providers/llama/thinking.py). Keep
    # the value as-is on every hop - coercing it with bool() would erase the level.
    enable_thinking: Any = False
    provider: Optional[BaseProvider] = None
    # The finished system prompt for this turn, delivered by the SERVER
    # (identity + tool contract, or identity only for plain chat).
    system_prompt: str = ""

    # File handling
    image_data: List[str] = field(default_factory=list)
    image_paths: List[str] = field(default_factory=list)
    file_contents: List[Dict] = field(default_factory=list)

    # WebSocket for streaming
    websocket: Any = None
    conversation_id: str = None
    stop_event: Optional[asyncio.Event] = None

    # (2026-08-23 session stats) active-time timer for THIS turn. Created by the chat bus
    # before dispatch; paused while user-decision menus are open so reported session time
    # only counts real working time. last_session_sec carries the PREVIOUS turn's duration
    # (read from the channel) so the UI can show "last session" alongside the live one.
    session_timer: Optional["SessionTimer"] = None
    last_session_sec: float = 0.0

    # (2026-10-23 live steering) Per-turn source of user messages typed WHILE the agent
    # works. `steer_source` is a ChatChannel-like object exposing has_pending_steer() and
    # drain_steer(); `on_steer_applied` is an async callback (item -> None) used by the
    # turn runner to persist an applied message exactly once. Both are None for callers
    # that do not support steering (normal mode, tests, direct API use).
    steer_source: Any = None
    on_steer_applied: Any = None

    # (2026-11-08 loop history) This turn's app.cycle_recorder.CycleRecorder, created by the
    # chat bus when the turn starts. Every step / tool / consent / steer / AI text of the run
    # is recorded on it and streamed as `cycle_*` frames, so the UI renders ONE collapsed
    # block per turn instead of a flat chat. None for callers without a bus (tests, CLI).
    cycle: Any = None
    
# (2026-10-11) FINAL TURN STATS HYGIENE: react_loop.py splices a "Loop stats:" line into the
# persisted answer right above 'Agentic AI: TASK DONE' and the UI renders it from that text.
# That line must NEVER be sent back to the model: when past answers carrying it were fed as
# history, the model copied an OLD stats line verbatim into its new answer (few-shot
# self-imitation), so a bubble could show TWO lines - one copied + one freshly spliced.
# build_messages() strips them from history; react_loop strips any residual copy from the
# model's own response before it is accumulated. The regex matches ONLY the exact spliced
# shape ("Loop stats: N / M | ...") so prose that merely mentions the feature survives.
_TURN_STATS_LINE_RE = re.compile(r"(?m)^[ \t]*Loop stats:\s*\d+\s*/\s*\d+[^\n]*$")


def strip_turn_stats_lines(text: str) -> str:
    """Remove spliced "Loop stats:" lines from a text (exact shape only).

    Collapses the blank lines left behind into one paragraph break; returns the input as-is
    when no such line exists.
    """
    if not isinstance(text, str) or "Loop stats:" not in text:
        return text
    cleaned = _TURN_STATS_LINE_RE.sub("", text)
    if cleaned == text:
        return text
    # Leading stats line(s) are stripped too - drop the blank lines they leave at the top.
    return re.sub(r"\n{3,}", "\n\n", cleaned).lstrip("\n")


def strip_turn_stats_from_messages(messages: List[Dict]) -> int:
    """Strip spliced "Loop stats:" lines from assistant history messages, in place.

    build_messages() calls this so the model never sees a past turn's stats line. The UI is
    unaffected - it renders the persisted text. Returns the number of lines removed.
    """
    removed = 0
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or "Loop stats:" not in content:
            continue
        cleaned = strip_turn_stats_lines(content)
        if cleaned != content:
            removed += len(_TURN_STATS_LINE_RE.findall(content))
            msg["content"] = cleaned
            try:
                from utils.history_manager import invalidate_message_tokens
                invalidate_message_tokens(msg)  # content mutated -> refresh cached token count
            except Exception:
                pass
    return removed


def build_messages(
    system_prompt: str,
    conversation_history: List[Dict],
    user_message_content: str,
    image_data: Optional[List[str]] = None,
) -> List[Dict]:
    """Build the provider message list: [system] + history[:-1] + current user message.

    image_data attaches base64 pixels to the final user message. That message carries
    _protect: True so utils/history_trimmer.safe_trim_history can never remove it.
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation_history[:-1])  # Exclude last user message from history

    # (2026-10-11) Keep past turns' spliced "Loop stats:" lines out of the model's view -
    # see strip_turn_stats_from_messages(). Without this the model copies an old line into
    # its new answer and a bubble ends up with two stats lines.
    strip_turn_stats_from_messages(messages)

    current_message = {"role": "user", "content": user_message_content}
    # `_protect` marks the CURRENT USER PROMPT for the atomic history trimmer:
    # it must remain in `messages` at all times (alongside the system prompt).
    # It is internal metadata only - the SERVER strips it in build_llama_payload().
    current_message["_protect"] = True
    if image_data:
        current_message["images"] = image_data
    messages.append(current_message)

    return messages


def strip_images_from_messages(messages: List[Dict]) -> int:
    """Remove the 'images' key (base64 blobs) from every message, in place.

    Fallback for a model with no multimodal projector: llama.cpp rejects image input with
    HTTP 500, but the prompt already lists the saved filenames, so the model can still name
    the files and read them via OCR. Drops each stripped message's cached _est_tokens so the
    budget stops charging for pixels that are gone. Returns the number of blobs removed.
    """
    removed = 0
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("images"):
            removed += len(msg["images"])
            del msg["images"]
            try:
                from utils.history_manager import invalidate_message_tokens
                invalidate_message_tokens(msg)  # images gone -> cached count is stale
            except Exception:
                pass  # counting still works, just uncached for this message
    return removed


MAX_TOOL_IMAGE_B64_CHARS = 2 * 1024 * 1024  # ~1.5MB raw image - cap so one screenshot can't eat the context
MAX_ATTACHED_IMAGES_PER_TURN = 8            # total images (user-uploaded + tool-viewed) riding on one turn


def attach_images_to_current_user_message(messages: List[Dict], new_b64_list: List[str]) -> int:
    """Attach tool-viewed base64 images to the protected CURRENT user message.

    Keeps exactly one copy in flight: it rides the next provider call, then
    sanitize_images_in_history strips it when the turn is persisted. Dedupes by base64
    content and skips oversized blobs.
    """
    if not messages or not new_b64_list:
        return 0

    # The protected current user prompt = last role=user message tagged _protect
    target = None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("_protect"):
            target = msg
            break
    if target is None:
        # Fallback: last user message (defensive - _protect should always be present)
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                target = msg
                break
    if target is None:
        return 0

    existing = list(target.get("images", []))
    added = 0
    for b64 in new_b64_list:
        if len(existing) >= MAX_ATTACHED_IMAGES_PER_TURN:
            logger.warning(
                "Image re-attach cap reached (%d images on current turn) - skipping further tool images",
                MAX_ATTACHED_IMAGES_PER_TURN,
            )
            break
        if not isinstance(b64, str) or len(b64) < 100:
            continue
        if len(b64) > MAX_TOOL_IMAGE_B64_CHARS:
            logger.warning(
                "Skipping oversized tool image for re-attach (%d chars > %d cap)",
                len(b64), MAX_TOOL_IMAGE_B64_CHARS,
            )
            continue
        if b64 in existing:
            continue  # dedupe - same pixels already attached this turn
        existing.append(b64)
        added += 1

    if added:
        target["images"] = existing
        from utils.history_manager import invalidate_message_tokens
        try:
            invalidate_message_tokens(target)  # images changed -> refresh cached token count
        except Exception:
            pass
    return added


def is_vision_unavailable_error(error: BaseException) -> bool:
    """True when the provider says image input is not supported (no mmproj).

    Matches both the clean VisionNotAvailableError message and any RuntimeError
    that carries the same text after crossing the WebSocket relay pipe.
    """
    return "vision is not available" in str(error).lower()


async def push_token_stats(websocket: Any, stats: Any, loop_stats: Optional[Dict] = None):
    """Push token statistics to the UI over the per-user WebSocket.

    send_token_stats is imported lazily to keep the logic <-> app.websocket import graph
    acyclic. loop_stats carries the agentic react-loop counters for the UI.
    """
    if not websocket or not stats:
        return

    # FIXED: Lazy import to break circular import chain
    from app.websocket.message_types import send_token_stats
    await send_token_stats(websocket, stats, loop_stats)


def check_stop_event(config: Any) -> Optional[str]:
    """Check if the user triggered a stop event.

    Args:
        config: Mode config with optional stop_event attribute.

    Returns:
        "Generation stopped by user." if stop was triggered, None otherwise.
    """
    if config.stop_event and config.stop_event.is_set():
        return "Generation stopped by user."
    return None


def build_session_stats(config: Any) -> Dict[str, float]:
    """Per-turn session stats for the UI status bar.

    Keys: "session_elapsed_sec" (active seconds of the current turn, pauses excluded) and
    "last_session_sec" (the previous completed turn). Cumulative generated tokens are counted
    in react_loop.py; normal mode reports its single call's TokenStats directly.
    """
    elapsed = 0.0
    timer = getattr(config, "session_timer", None)
    if timer is not None:
        try:
            elapsed = max(0.0, timer.elapsed())
        except Exception:
            elapsed = 0.0
    last_sec = float(getattr(config, "last_session_sec", 0.0) or 0.0)
    return {"session_elapsed_sec": round(elapsed, 1), "last_session_sec": round(last_sec, 1)}
