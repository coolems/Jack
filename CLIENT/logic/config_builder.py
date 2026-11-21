"""Configuration builders for normal and agentic chat modes.

build_normal_config() / build_agentic_config() construct the mode config objects with
defaults from config.py - no hardcoded values here.
"""

from typing import List, Dict, Optional, Any
import asyncio

from client_config import MODEL_NAME, PROVIDER_DEFAULT_TEMPERATURE
from app.providers import BaseProvider


def build_normal_config(
    *,
    model: str = MODEL_NAME,
    temperature: float = PROVIDER_DEFAULT_TEMPERATURE,
    # enable_thinking: bool or canonical level ("none"/"low"/"medium"/"high").
    enable_thinking: Any = False,
    provider: Optional[BaseProvider] = None,
    system_prompt: str = "",
    image_data: List[str] | None = None,
    image_paths: List[str] | None = None,
    file_contents: List[Dict] | None = None,
    websocket: Any = None,
    conversation_id: str | None = None,
    stop_event: Optional[asyncio.Event] = None,
    session_timer=None,   # (2026-08-23) active-time timer for this turn (pauses on user menus)
) -> "NormalModeConfig":
    """Build a NormalModeConfig with defaults from config.py."""
    # Lazy import to break circular dependency chain
    from logic.normal import NormalModeConfig  # noqa: PLC0415
    return NormalModeConfig(
        model=model,
        temperature=temperature,
        enable_thinking=enable_thinking,
        provider=provider,
        system_prompt=system_prompt,
        image_data=image_data or [],
        image_paths=image_paths or [],
        file_contents=file_contents or [],
        websocket=websocket,
        conversation_id=conversation_id,
        stop_event=stop_event,
        session_timer=session_timer,
    )


def build_agentic_config(
    *,
    model: str = MODEL_NAME,
    temperature: float = PROVIDER_DEFAULT_TEMPERATURE,
    # enable_thinking: bool or canonical level ("none"/"low"/"medium"/"high").
    enable_thinking: Any = False,
    provider: Optional[BaseProvider] = None,
    system_prompt: str = "",
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
    steer_source: Any = None,       # (2026-10-23) live mid-run steering queue (ChatChannel)
    on_steer_applied: Any = None,   # async callback: persist one applied steering message
    cycle: Any = None,              # (2026-11-08) this turn's app.cycle_recorder.CycleRecorder
) -> "AgenticModeConfig":
    """Build an AgenticModeConfig with defaults from config.py."""
    # Lazy import to break circular dependency chain
    from logic.agentic import AgenticModeConfig  # noqa: PLC0415
    return AgenticModeConfig(
        model=model,
        temperature=temperature,
        enable_thinking=enable_thinking,
        provider=provider,
        system_prompt=system_prompt,
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
        steer_source=steer_source,
        on_steer_applied=on_steer_applied,
        cycle=cycle,
    )
