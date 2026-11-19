"""COOLEMS Pydantic Schemas - Input validation for WebSocket and API endpoints."""
from pydantic import BaseModel, Field, field_validator
from typing import Any, List, Optional

# Thinking on the wire: a bool ("show reasoning or not") or a canonical level picked in
# the UI. Levels are the only vocabulary sent - each model's own effort names
# (xhigh / medium / low / thinking_budget...) are decided by the serving endpoint's
# ThinkingPolicy from its chat template, so a client never sends a model-specific value.
THINKING_MODES = ("none", "low", "medium", "high")
_THINKING_ALIASES = {
    "off": "none", "false": "none", "disabled": "none",
    "on": "medium", "auto": "medium", "true": "medium", "think": "medium",
    "xhigh": "high", "highest": "high", "max": "high",
    "minimal": "low", "brief": "low",
}


def normalize_thinking(value) -> Any:
    """Canonical thinking level (str) or bool for anything the client sent.

    An empty or unknown value becomes '' (no instruction): the SERVER then sends no thinking
    keys and the model keeps its own default instead of getting a value its template rejects.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    raw = str(value).strip().lower()
    if raw in THINKING_MODES:
        return raw
    if raw in _THINKING_ALIASES:
        return _THINKING_ALIASES[raw]
    return ""


class WebSocketChatMessage(BaseModel):
    """Validated schema for incoming WebSocket chat messages.
    """
    
    message: str = Field(
        ...,
        min_length=1,
        max_length=950000,
        description="The user's chat message text"
    )
    model: Optional[str] = Field(
        None,
        max_length=200,
        description="Model name override (defaults to server config)"
    )
    system_prompt: Optional[str] = Field(
        None,
        max_length=5000,
        description="Custom system prompt override"
    )
    agent_mode: bool = Field(
        False,
        description="Whether to run in agentic/tool-use mode"
    )
    media_files: List[str] = Field(
        default_factory=list,
        max_length=50,
        description="List of file URLs/paths attached to the message"
    )
    # Legacy field, kept for backward compatibility with old UI builds (they sent a
    # {"wikipedia": true, ...} dict). The engine list itself lives in
    # <CLIENT>/client_config/search_engines.json (single source of truth); the web_search tool
    # reads that file live on every search. New clients send nothing here - any shape is
    # tolerated so old clients never break validation.
    search_engines: Optional[Any] = Field(
        None,
        description="Legacy field (old UI builds only) - engine config lives in config/search_engines.json"
    )
    enable_thinking: Any = Field(
        False,
        description="Thinking: legacy bool (show reasoning) or a canonical level "
                    "'none' | 'low' | 'medium' | 'high' chosen from the model's capabilities"
    )
    # (2026-10-23 live steering) While an agentic turn is running the UI sends a normal
    # message payload with steer=True instead of submitting a new turn. The text is queued
    # on the chat and injected into the running ReAct loop at its next safe boundary.
    steer: bool = Field(
        False,
        description="True when this message steers the currently running agentic task instead of starting a new turn"
    )
    steer_id: Optional[str] = Field(
        None,
        max_length=100,
        description="Client-generated correlation id echoed back in steer_received/applied/rejected frames"
    )
    
    @field_validator("message")
    @classmethod
    def message_must_not_be_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Message cannot be whitespace-only")
        return v

    @field_validator("model")
    @classmethod
    def model_must_be_valid_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            # Basic sanity: model names shouldn't contain path traversal or control chars
            if ".." in v or "\0" in v:
                raise ValueError("Model name contains invalid characters")
            # Allow alphanumeric, colons, slashes, dots, hyphens, underscores
            import re
            if not re.match(r'^[a-zA-Z0-9:/._\-]+$', v.strip()):
                raise ValueError("Model name contains invalid characters")
            return v.strip()
        return v

    @field_validator("media_files")
    @classmethod
    def media_files_must_be_safe(cls, v: List[str]) -> List[str]:
        for f in v:
            if ".." in f or "\0" in f:
                raise ValueError("File path contains invalid characters")
        return v

    @field_validator("enable_thinking")
    @classmethod
    def enable_thinking_must_be_bool_or_level(cls, v: Any) -> Any:
        """Accept a bool or a level name; anything else means "no instruction".

        Never raises: a typo must not reject the whole message, it only leaves the model's own
        thinking default in place.
        """
        return normalize_thinking(v)

# Aliases for backward compatibility
