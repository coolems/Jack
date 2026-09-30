"""
Shared base code for normal and agentic chat modes.

Contains:
- BaseModeConfig: Common configuration dataclass (12 shared fields)
- build_messages(): Shared message list builder (system + history + user + images)
- strip_images_from_messages(): Remove 'images' keys so a non-vision model can still answer
- attach_images_to_current_user_message(): Re-inject tool-viewed base64 pixels into the loop
- is_vision_unavailable_error(): Detect "no mmproj" provider errors
- push_token_stats(): Async helper to send token stats via WebSocket
"""

import logging
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
import asyncio

from config import MODEL_NAME, PROVIDER_DEFAULT_TEMPERATURE
from app.providers import BaseProvider


logger = logging.getLogger("COOLEMS.Logic.Base")


@dataclass
class BaseModeConfig:
    """Base configuration shared by both normal and agentic chat modes"""
    model: str = MODEL_NAME
    temperature: float = PROVIDER_DEFAULT_TEMPERATURE
    enable_thinking: bool = False
    provider: Optional[BaseProvider] = None
    agent_name: str = "Jack"
    dna_content: str = ""

    # File handling
    image_data: List[str] = field(default_factory=list)
    image_paths: List[str] = field(default_factory=list)
    file_contents: List[Dict] = field(default_factory=list)

    # WebSocket for streaming
    websocket: Any = None
    conversation_id: str = None
    stop_event: Optional[asyncio.Event] = None


def build_messages(
    system_prompt: str,
    conversation_history: List[Dict],
    user_message_content: str,
    image_data: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Build the messages list for provider calls.

    Standard pattern used by both normal and agentic modes:
        [system_prompt] + history[:-1] + current_user_message(with images?)

    Args:
        system_prompt: The full system prompt text.
        conversation_history: Full conversation history (includes last user message at end).
        user_message_content: The enhanced user message text for the current turn.
        image_data: Optional list of base64-encoded images to attach.

    Returns:
        List of message dicts ready for provider call. The final (current user)
        prompt carries `_protect: True` so the agentic atomic trimmer can never
        remove it - see utils/history_trimmer.safe_trim_history().
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation_history[:-1])  # Exclude last user message from history

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
    """Remove the Ollama-style 'images' key (base64 blobs) from every message.

    Used as a self-healing fallback when the loaded model has NO multimodal
    projector (mmproj): llama.cpp rejects any image input with HTTP 500, which
    would otherwise kill the whole turn even though the text question is fine.
    The prompt text already lists the saved filenames of attached images, so
    after stripping the model can still see WHICH files exist and answer about
    them (or transcribe them via OCR) without pixels.

    Args:
        messages: Message list as built by build_messages() (mutated in place).

    Returns:
        Number of base64 image blobs removed.
    """
    removed = 0
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("images"):
            removed += len(msg["images"])
            del msg["images"]
    return removed


MAX_TOOL_IMAGE_B64_CHARS = 2 * 1024 * 1024  # ~1.5MB raw image - cap so one screenshot can't eat the context
MAX_ATTACHED_IMAGES_PER_TURN = 8            # total images (user-uploaded + tool-viewed) riding on one turn


def attach_images_to_current_user_message(messages: List[Dict], new_b64_list: List[str]) -> int:
    """Re-attach base64 images (from tool results like view_image) to the protected
    CURRENT user message so the NEXT provider call carries real pixels.

    Without this, a tool result containing 'image_base64' is sanitized out of history
    (to protect the context window) and NEVER reaches the model - the model only ever
    saw the filename reference. Attaching to the protected current-user prompt keeps
    exactly ONE copy in flight: it rides on the next provider call, then gets stripped
    by sanitize_images_in_history() when that turn is persisted/loaded back.

    Dedupes by base64 content and caps size per image (oversized blobs are skipped).

    Args:
        messages: Message list as maintained by the ReAct loop (mutated in place).
        new_b64_list: Raw base64 strings to attach (no MIME prefix - the SERVER's
            convert_messages_for_vision() adds data:image/jpeg;base64, on send).

    Returns:
        Number of images actually attached.
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
    """
    Push token statistics to the UI via WebSocket.

    Lazy-imports send_token_stats to avoid circular imports:
        logic -> app.websocket.message_types -> app.websocket.__init__ -> handler -> from logic import ... (CIRCULAR!)

    Args:
        websocket: The active WebSocket connection (per-user).
        stats: TokenStats object returned by provider.chat_stream().
        loop_stats: Optional dict with react_loop counters for agentic mode UI display.
                    Contains "loop_tools" and "loop_no_tools".
    """
    if not websocket or not stats:
        return

    # FIXED: Lazy import to break circular import chain
    from app.websocket.message_types import send_token_stats
    await send_token_stats(websocket, stats, loop_stats)


def check_stop_event(config: Any) -> Optional[str]:
    """
    Check if the user triggered a stop event.

    Args:
        config: Mode config with optional stop_event attribute.

    Returns:
        "Generation stopped by user." if stop was triggered, None otherwise.
    """
    if config.stop_event and config.stop_event.is_set():
        return "Generation stopped by user."
    return None
