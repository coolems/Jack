"""Token statistics data structures and WebSocket helpers.

TokenStats captures real token counts from the provider response (llama.cpp). Stats
travel with the chat_stream() return value and are pushed to the UI over the caller's
own WebSocket, so concurrent users never mix.

Key design decisions:
    - TokenStats is a frozen dataclass (immutable after creation).
    - All fields have defaults, so providers fill what they have.
    - Speed is derived from tokens + duration when both are available.
    - The 'token_stats' message type targets the socket that owns the conversation.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("COOLEMS.Provider.TokenStats")

@dataclass(frozen=True)
class TokenStats:
    """Immutable token statistics from a single AI provider call."""

    prompt_tokens: int = 0
    generated_tokens: int = 0
    truncated: bool = False
    generation_speed: Optional[float] = None
    total_duration: float = 0.0
    provider_name: str = ""
    model: str = ""
    context_window: int = 131072

    # Derived properties
    @property
    def tokens_remaining(self) -> int:
        """Tokens remaining in context window after this exchange."""
        return max(0, self.context_window - self.prompt_tokens - self.generated_tokens)

    @property
    def calculated_speed(self) -> Optional[float]:
        """Auto-calculated speed if not explicitly set."""
        if self.generation_speed is not None:
            return self.generation_speed
        if self.total_duration > 0 and self.generated_tokens > 0:
            return round(self.generated_tokens / self.total_duration, 1)
        return None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization / WebSocket sending."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "generated_tokens": self.generated_tokens,
            "truncated": self.truncated,
            "generation_speed": self.calculated_speed,
            "total_duration": round(self.total_duration, 2),
            "provider_name": self.provider_name,
            "model": self.model,
            "context_window": self.context_window,
            "tokens_remaining": self.tokens_remaining,
        }
