"""
COOLEMS Pydantic Schemas - Input validation for WebSocket and API endpoints.
"""
from pydantic import BaseModel, Field, field_validator
from typing import Any, Dict, List, Optional

class WebSocketChatMessage(BaseModel):
    """
    Validated schema for incoming WebSocket chat messages from the client.
    
    Replaces raw json.loads() + .get() pattern with strict validation.
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
    # <CLIENT>/config/search_engines.json (single source of truth); the web_search tool
    # reads that file live on every search. New clients send nothing here - any shape is
    # tolerated so old clients never break validation.
    search_engines: Optional[Any] = Field(
        None,
        description="Legacy field (old UI builds only) - engine config lives in config/search_engines.json"
    )
    enable_thinking: bool = Field(
        False,
        description="Whether to enable model thinking/reasoning mode"
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

class WebSocketErrorMessage(BaseModel):
    """Schema for error responses sent back to the client."""
    type: str = "error"
    content: str
    code: Optional[int] = None

class WebSocketSystemMessage(BaseModel):
    """Schema for system/status messages."""
    type: str = "system"
    content: str

class WebSocketContentMessage(BaseModel):
    """Schema for streamed content chunks."""
    type: str = "content"
    content: str

class WebSocketToolStartMessage(BaseModel):
    """Schema for tool invocation start notifications."""
    type: str = "tool_start"
    tool: str
    input: str

class WebSocketToolEndMessage(BaseModel):
    """Schema for tool invocation end notifications."""
    type: str = "tool_end"
    tool: str
    output: str

class WebSocketDoneMessage(BaseModel):
    """Schema for generation complete signal."""
    type: str = "done"

class WebSocketThinkingMessage(BaseModel):
    """Schema for streamed thinking/reasoning chunks."""
    type: str = "thinking"
    content: str

class WebSocketProviderErrorMessage(BaseModel):
    """Schema for provider (Ollama/llama.cpp) error notifications."""
    type: str = "provider_error"
    error_type: str
    content: str

class WebSocketExecutionFailMessage(BaseModel):
    """Schema for execution failure notifications."""
    type: str = "execution_fail"
    code: int
    error_type: str
    content: str

class WebSocketImageGeneratedMessage(BaseModel):
    """Schema for generated image notifications."""
    type: str = "image_generated"
    image_base64: str
    file_path: Optional[str] = None
    filename: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None

class WebSocketSearchWebMessage(BaseModel):
    """Schema for search-web link suggestions."""
    type: str = "search_web"
    query: str
    link_text: str

# Aliases for backward compatibility
ChatMessage = WebSocketChatMessage
