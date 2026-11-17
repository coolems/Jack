"""Normal chat mode - a standard conversation without tools.

Builds the system prompt with agent DNA, processes images and file contents, streams the
provider response and pushes TokenStats to the UI over WebSocket. Each WebSocket belongs
to one user/session, so stats are per request with no shared globals.
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

    Pushes the returned TokenStats to the UI over WebSocket. config's stop_event is passed to
    the provider so the red stop button aborts the streaming loop immediately.
    """
    # The SERVER delivers the prompt for this turn (identity + identity lock);
    # plain chat carries no tool contract.
    messages = build_messages(
        system_prompt=config.system_prompt,
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
    # enable_thinking: bool or canonical level, forwarded verbatim.
    enable_thinking: Any = False,
    conv_id: str = None,
    tools: list = None,
    provider=None,
    ollama_url: str = None,  # deprecated, ignored in CLIENT
    stop_event=None,
) -> str:
    """Stream one normal-mode turn through the active provider.

    Uses the given provider or the ProviderManager active provider, then pushes TokenStats to
    the UI. ollama_url is unused in CLIENT mode.
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
    """Non-streaming completion through the active provider.

    Uses the given provider or the ProviderManager active provider. ollama_url is unused in
    CLIENT mode.
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
    """Resolve the active provider: explicit argument first, then the ProviderManager
    singleton. Raises RuntimeError when neither is available.
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
