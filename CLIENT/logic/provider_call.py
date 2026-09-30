"""
Provider call handling with automatic restart on connection errors.

UPDATED (2026-08-24): Vision self-healing fallback — when the loaded model has
no multimodal projector (mmproj), llama.cpp rejects ANY image input with HTTP 500.
Instead of killing the whole agentic turn, we strip the base64 images from the
message list and retry once: the prompt text already lists every attached file's
saved name, so the model can still work (e.g. via transcribe_image OCR) without pixels.
"""

import asyncio
import json
import logging
import time
from typing import List, Dict, Any, Tuple

logger = logging.getLogger("COOLEMS.Logic.ProviderCall")


async def call_provider_with_retry(config: Any, messages: List[Dict]) -> Tuple[Any, Any]:
    """
    Call provider with automatic restart on connection error.

    Args:
        config: AgenticModeConfig instance (provider, websocket, etc.).
        messages: Messages list to send to provider.

    Returns:
        Tuple of (result, stats) from provider.chat_stream()
    """
    vision_fallback_used = False
    for attempt in range(2):
        try:
            provider_call_start = time.time()

            result, stats = await config.provider.chat_stream(
                config.model,
                messages,
                config.websocket,
                temperature=config.temperature,
                enable_thinking=config.enable_thinking,
                conv_id=config.conversation_id,
                tools=config.ollama_tools,
                stop_event=config.stop_event,
            )

            provider_call_elapsed = time.time() - provider_call_start

            if isinstance(result, dict):
                logger.info(f"[AGENTIC.DEBUG] Provider returned dict with keys: {list(result.keys())}")

            return result, stats

        except Exception as err:
            # ---- VISION SELF-HEALING FALLBACK (2026-08-24) -------------------
            # Model has no mmproj -> llama.cpp 500 "image input is not supported".
            # Strip images and retry ONCE so the text question still gets answered.
            from .base_mode import strip_images_from_messages, is_vision_unavailable_error

            if (not vision_fallback_used
                    and attempt == 0
                    and is_vision_unavailable_error(err)):
                removed = strip_images_from_messages(messages)
                vision_fallback_used = True
                logger.warning(
                    f"[AGENTIC.DEBUG] Vision unavailable for model '{config.model}' "
                    f"({removed} base64 image(s) stripped). Retrying text-only so the turn survives."
                )
                try:
                    await config.websocket.send_text(json.dumps({
                        "type": "system",
                        "content": ("Vision is not available for this model (no mmproj projector loaded). "
                                    "Continuing without pixels - attached image filenames are still listed in the prompt, "
                                    "so I can transcribe them via OCR if needed."),
                    }))
                except Exception:
                    logger.debug("WebSocket send failed during vision-fallback notice")
                continue  # retry with stripped messages

            # ---- CONNECTION ERROR RESTART (original behavior) -----------------
            if not isinstance(err, ConnectionError):
                raise

            conn_elapsed = time.time() - provider_call_start
            logger.error(f"[AGENTIC.DEBUG] ConnectionError after {conn_elapsed:.2f}s: {err}")

            if attempt == 0:
                restart_msg = f"Warning: {config.provider.name} is not running. Attempting automatic restart..."
                await config.websocket.send_text(json.dumps({
                    "type": "system",
                    "content": restart_msg
                }))

                logger.info("[AGENTIC.DEBUG] Calling provider.start_server()")
                # (2026-09 threadless refactor): the coolems_client start_server() is a trivial non-blocking
                # no-op (it only logs - there is nothing local to start), so it is called directly instead of
                # being pushed onto a worker thread via to_thread.
                restarted = config.provider.start_server()

                if restarted:
                    logger.info(f"[AGENTIC.DEBUG] {config.provider.name} restarted successfully, retrying...")
                    success_msg = f"{config.provider.name} restarted. Resending request..."
                    await config.websocket.send_text(json.dumps({
                        "type": "system",
                        "content": success_msg
                    }))
                    continue  # Retry the call
                else:
                    logger.error("[AGENTIC.DEBUG] Failed to restart provider")
                    raise err
            else:
                logger.error("[AGENTIC.DEBUG] Second attempt also failed, raising ConnectionError")
                raise err