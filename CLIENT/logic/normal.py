"""
Normal chat mode logic - standard AI conversation without tools.

Handles:
- Building system prompt with agent DNA
- Processing images and file contents
- Streaming responses from the active provider (CoolemsClientProvider)
- Pushing token stats to the UI via WebSocket (bulletproof per-user)
- Error handling and fallbacks

TOKEN STATISTICS (Bulletproof Per-Request):
    After each chat_stream() call, the returned TokenStats is pushed to
    the UI via WebSocket using send_token_stats(). This is bulletproof
    because each WebSocket connection is tied to exactly one user/session.
    No shared globals are used for per-user data.
"""


import logging
from dataclasses import dataclass
from typing import List, Dict, Any


from .base_mode import (
    BaseModeConfig,
    build_messages,
    strip_images_from_messages,
    is_vision_unavailable_error,
    push_token_stats,
    check_stop_event,
    build_session_stats,
)



logger = logging.getLogger("COOLEMS.Logic.Normal")



@dataclass
class NormalModeConfig(BaseModeConfig):
    """Configuration for normal chat mode. Inherits all shared fields from BaseModeConfig."""
    pass



async def normal_mode(
    config: NormalModeConfig,
    conversation_history: List[Dict],
    enhanced_message: str,
    media_files: List[str] = None
) -> str:
    """Run normal chat mode (no tools).

    After the provider returns, pushes TokenStats to the UI via WebSocket
    so the user sees real token counts immediately. No polling needed.

    The stop_event from config is passed through to the provider so that
    when a user clicks the red stop button, the streaming loop inside
    the provider aborts immediately.
    """
    # Build enhanced system prompt with DNA
    enhanced_system_prompt = f"""{config.dna_content}

## RESPONSE RULES
- You are {config.agent_name}. This is your identity.
- Never introduce yourself as anything else.
"""

    messages = build_messages(
        system_prompt=enhanced_system_prompt,
        conversation_history=conversation_history,
        user_message_content=f"\n{enhanced_message}",
        image_data=config.image_data,
    )

    if config.image_data:
        logger.info(f"Sending {len(config.image_data)} image(s) to provider vision API")

    # Stream the response via provider - now returns (response, TokenStats).
    # VISION SELF-HEALING FALLBACK (2026-08-24): if the loaded model has no mmproj,
    # llama.cpp rejects any image input with HTTP 500. Strip the base64 images and
    # retry once so the text question still gets answered (the prompt already lists
    # every attached file's saved name for later reference / OCR).
    try:
        final_response, stats = await config.provider.chat_stream(
            config.model,
            messages,
            config.websocket,
            temperature=config.temperature,
            enable_thinking=config.enable_thinking,
            conv_id=config.conversation_id,
            tools=None,
            stop_event=config.stop_event,
        )
    except Exception as err:
        if not is_vision_unavailable_error(err):
            raise
        removed = strip_images_from_messages(messages)
        logger.warning(
            f"Vision unavailable for model '{config.model}' ({removed} base64 image(s) stripped). "
            f"Retrying text-only so the turn survives."
        )
        final_response, stats = await config.provider.chat_stream(
            config.model,
            messages,
            config.websocket,
            temperature=config.temperature,
            enable_thinking=config.enable_thinking,
            conv_id=config.conversation_id,
            tools=None,
            stop_event=config.stop_event,
        )

    # Push token stats to the UI via WebSocket - bulletproof per-user delivery.
    # (2026-08-23 session stats) normal mode has exactly ONE provider call per turn, so
    # the cumulative 'session generated tokens' is this call's count; elapsed/last times
    # come from the turn's SessionTimer (approval pauses excluded - none occur in normal).
    _sess_stats = build_session_stats(config)
    await push_token_stats(config.websocket, stats, {
        "session_generated_tokens": int(getattr(stats, "generated_tokens", 0) or 0),
        "session_elapsed_sec": _sess_stats["session_elapsed_sec"],
        "last_session_sec": _sess_stats["last_session_sec"],
    })

    # If stop was triggered during streaming, return early message
    stopped = check_stop_event(config)
    if stopped:
        return stopped

    return final_response



# ---- Backward-compatible wrappers (used by websocket.py for /search) ----


async def stream_ollama(
    model: str,
    messages: List[Dict],
    websocket: Any,
    temperature: float = None,
    timeout: int = None,
    enable_thinking: bool = False,
    conv_id: str = None,
    tools: list = None,
    provider=None,
    ollama_url: str = None,  # deprecated, ignored in CLIENT
    stop_event=None,
) -> str:
    """Backward-compatible streaming wrapper.

    Uses the provided provider or falls back to ProviderManager active provider.
    Now pushes TokenStats to the UI via WebSocket after the call.

    Args:
        ollama_url: Deprecated and ignored in CLIENT mode. Kept for API compatibility.
    """
    from config import PROVIDER_DEFAULT_TEMPERATURE
    if temperature is None:
        temperature = PROVIDER_DEFAULT_TEMPERATURE

    p = _resolve_provider(provider)
    result, stats = await p.chat_stream(
        model, messages, websocket,
        temperature=temperature,
        enable_thinking=enable_thinking,
        conv_id=conv_id,
        tools=tools,
        stop_event=stop_event,
    )
    await push_token_stats(websocket, stats)
    return result



async def call_ollama_non_streaming(
    model: str,
    messages: List[Dict],
    temperature: float = None,
    timeout: int = None,
    provider=None,
    ollama_url: str = None,  # deprecated, ignored in CLIENT
) -> str:
    """Backward-compatible non-streaming wrapper.

    Uses the provided provider or falls back to ProviderManager active provider.

    Args:
        ollama_url: Deprecated and ignored in CLIENT mode. Kept for API compatibility.
    """
    from config import PROVIDER_DEFAULT_TEMPERATURE
    if temperature is None:
        temperature = PROVIDER_DEFAULT_TEMPERATURE

    p = _resolve_provider(provider)
    result = await p.chat_non_streaming(
        model, messages,
        temperature=temperature,
    )
    return result



def get_ollama_status_message(
    error: Exception,
    model_name: str = None,
    timeout: int = 9300,
    provider=None,
) -> str:
    """Backward-compatible status message wrapper.

    Uses the provided provider or falls back to ProviderManager active provider."""
    from config import MODEL_NAME
    if model_name is None:
        model_name = MODEL_NAME

    p = _resolve_provider(provider)
    return p.status_message(error, model_name, timeout)



# ---- Internal helper for backward-compat wrappers ----


def _resolve_provider(provider):
    """Resolve the active provider.

    Priority:
      1. Explicitly passed provider argument
      2. ProviderManager singleton (set at startup with CoolemsClientProvider)

    Raises RuntimeError if no provider is available - this should never happen
    in normal operation since code_client.py always sets up the provider.
    """
    if provider is not None:
        return provider

    from app.provider_manager import ProviderManager
    managed = ProviderManager.get_provider()
    if managed is not None:
        return managed

    raise RuntimeError(
        "No AI provider available for normal mode request. "
        "This should not happen - the CoolemsClientProvider must be initialized at startup. "
        "Check that code_client.py completed initialization successfully."
    )
