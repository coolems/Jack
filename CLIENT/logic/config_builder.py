"""
Configuration builders for normal and agentic chat modes.

Single source of truth for constructing NormalModeConfig and AgenticModeConfig objects.
All default values are pulled from config.py — no hardcoded magic numbers here.

Usage:
    from logic.config_builder import build_normal_config, build_agentic_config
    
    cfg = build_normal_config(
        provider=provider,
        agent=agent,
        websocket=ws,
        conversation_id="abc-123",
        stop_event=stop_evt,
        enable_thinking=False,
    )

"""

from typing import List, Dict, Optional, Any
import asyncio

from config import MODEL_NAME, PROVIDER_DEFAULT_TEMPERATURE
from app.providers import BaseProvider


def build_normal_config(
    *,
    model: str = MODEL_NAME,
    temperature: float = PROVIDER_DEFAULT_TEMPERATURE,
    enable_thinking: bool = False,
    provider: Optional[BaseProvider] = None,
    agent_name: str = "Jack",
    dna_content: str = "",
    image_data: List[str] | None = None,
    image_paths: List[str] | None = None,
    file_contents: List[Dict] | None = None,
    websocket: Any = None,
    conversation_id: str | None = None,
    stop_event: Optional[asyncio.Event] = None,
    session_timer=None,   # (2026-08-23) active-time timer for this turn (pauses on user menus)
    last_session_sec: float = 0.0,  # previous turn's duration for the UI "last session" stat
) -> "NormalModeConfig":
    """
    Build a NormalModeConfig with defaults from config.py.

    Args:
        model: Model name (default: MODEL_NAME from config).
        temperature: Generation temperature (default: PROVIDER_DEFAULT_TEMPERATURE from config).
        enable_thinking: Enable thinking/reasoning mode.
        provider: AI provider instance.
        agent_name: Agent identity name.
        dna_content: Agent DNA/system prompt content.
        image_data: List of base64-encoded images.
        image_paths: List of image file paths.
        file_contents: List of uploaded file content dicts.
        websocket: WebSocket connection for streaming.
        conversation_id: Conversation ID for DB tracking.
        stop_event: Asyncio event for user-initiated stop.

    Returns:
        Fully constructed NormalModeConfig instance.
    """
    # Lazy import to break circular dependency chain
    from logic.normal import NormalModeConfig  # noqa: PLC0415
    return NormalModeConfig(
        model=model,
        temperature=temperature,
        enable_thinking=enable_thinking,
        provider=provider,
        agent_name=agent_name,
        dna_content=dna_content,
        image_data=image_data or [],
        image_paths=image_paths or [],
        file_contents=file_contents or [],
        websocket=websocket,
        conversation_id=conversation_id,
        stop_event=stop_event,
        session_timer=session_timer,
        last_session_sec=last_session_sec,
    )


def build_agentic_config(
    *,
    model: str = MODEL_NAME,
    temperature: float = PROVIDER_DEFAULT_TEMPERATURE,
    enable_thinking: bool = False,
    provider: Optional[BaseProvider] = None,
    agent_name: str = "Jack",
    dna_content: str = "",
    generic_tool_prompt: str = "",
    image_data: List[str] | None = None,
    image_paths: List[str] | None = None,
    file_contents: List[Dict] | None = None,
    websocket: Any = None,
    conversation_id: str | None = None,
    stop_event: Optional[asyncio.Event] = None,
    tool_orchestrator: Any = None,
    ollama_tools: List[Dict] | None = None,
    api_key: str | None = None,
    session_timer=None,   # (2026-08-23) active-time timer for this turn (pauses on user menus)
    last_session_sec: float = 0.0,  # previous turn's duration for the UI "last session" stat
) -> "AgenticModeConfig":
    """
    Build an AgenticModeConfig with defaults from config.py.

    Args:
        model: Model name (default: MODEL_NAME from config).
        temperature: Generation temperature (default: PROVIDER_DEFAULT_TEMPERATURE from config).
        enable_thinking: Enable thinking/reasoning mode.
        provider: AI provider instance.
        agent_name: Agent identity name.
        dna_content: Agent DNA/system prompt content.
        generic_tool_prompt: Generic tool instruction prompt text.
        image_data: List of base64-encoded images.
        image_paths: List of image file paths.
        file_contents: List of uploaded file content dicts.
        websocket: WebSocket connection for streaming.
        conversation_id: Conversation ID for DB tracking.
        stop_event: Asyncio event for user-initiated stop.
        tool_orchestrator: Tool orchestrator instance.
        ollama_tools: Pre-built Ollama-compatible tool definitions.
        api_key: API key for role-based permission checking.

    Returns:
        Fully constructed AgenticModeConfig instance.
    """
    # Lazy import to break circular dependency chain
    from logic.agentic import AgenticModeConfig  # noqa: PLC0415
    return AgenticModeConfig(
        model=model,
        temperature=temperature,
        enable_thinking=enable_thinking,
        provider=provider,
        agent_name=agent_name,
        dna_content=dna_content,
        generic_tool_prompt=generic_tool_prompt,
        image_data=image_data or [],
        image_paths=image_paths or [],
        file_contents=file_contents or [],
        websocket=websocket,
        conversation_id=conversation_id,
        stop_event=stop_event,
        tool_orchestrator=tool_orchestrator,
        ollama_tools=ollama_tools or [],
        api_key=api_key,
        session_timer=session_timer,
        last_session_sec=last_session_sec,
    )
