"""Typed WebSocket send helpers.

Each helper builds a dict with a "type" field and sends it as JSON. send_token_stats()
targets one connection, and every connection belongs to exactly one conversation/user, so
concurrent users never mix.
"""

import json
import logging
from typing import Optional, Any, Dict

# (2026-08-20 dedup) lazy: the real TokenStats is delivered from SERVER with tools_response
# and installed in sys.modules before any WebSocket traffic exists. This import binds the
# PEP 562 placeholder pre-delivery; every call site runs post-init against the real class.
from app.providers import TokenStats

logger = logging.getLogger("COOLEMS.WebSocket")

async def send_system(websocket: Any, content: str):
    """Send a system/status message."""
    await websocket.send_text(json.dumps({"type": "system", "content": content}))

async def send_content(websocket: Any, content: str):
    """Send a content chunk."""
    await websocket.send_text(json.dumps({"type": "content", "content": content}))

async def send_tool_start(websocket: Any, tool: str, input_data: str):
    """Send tool invocation start notification."""
    await websocket.send_text(json.dumps({
        "type": "tool_start",
        "tool": tool,
        "input": input_data
    }))

async def send_tool_end(websocket: Any, tool: str, output: str):
    """Send tool invocation end notification."""
    await websocket.send_text(json.dumps({
        "type": "tool_end",
        "tool": tool,
        "output": output
    }))

async def send_done(websocket: Any):
    """Send generation complete signal."""
    await websocket.send_text(json.dumps({"type": "done"}))

async def send_error(websocket: Any, content: str, code: Optional[int] = None):
    """Send an error message."""
    msg = {"type": "error", "content": content}
    if code is not None:
        msg["code"] = code
    await websocket.send_text(json.dumps(msg))

async def send_provider_error(websocket: Any, error_type: str, content: str):
    """Send a provider-level error (connection, timeout, etc.)."""
    await websocket.send_text(json.dumps({
        "type": "provider_error",
        "error_type": error_type,
        "content": content
    }))

async def send_execution_fail(websocket: Any, code: int, error_type: str, content: str):
    """Send execution failure signal."""
    await websocket.send_text(json.dumps({
        "type": "execution_fail",
        "code": code,
        "error_type": error_type,
        "content": content
    }))

async def send_search_web_suggestion(websocket: Any, query: str, link_text: str):
    """Send a 'search same question on internet' suggestion."""
    await websocket.send_text(json.dumps({
        "type": "search_web",
        "query": query,
        "link_text": link_text
    }))

async def send_recovery_warning(websocket: Any, message: str, status: str = "warning"):
    """Send a recovery warning block to the UI.

    This creates a visible orange warning block in the chat that explains
    what happened when the server went down and the system recovered.

    Args:
        websocket: The FastAPI WebSocket connection to send to.
        message: The warning message content.
        status: One of 'warning', 'success', 'error' to control styling.
    """
    await websocket.send_text(json.dumps({
        "type": "recovery_warning",
        "message": message,
        "status": status
    }))
    
async def send_token_stats(websocket: Any, stats: TokenStats, session_stats: Optional[Dict] = None):
    """Send token statistics to one WebSocket client.

    Each connection is tied to exactly one conversation/user, so concurrent users never mix.
    The UI listens for the 'token_stats' message type and updates its token panel.

    Args:
        websocket: the FastAPI WebSocket connection to send to.
        stats: the TokenStats instance.
        session_stats: optional dict with per-turn session fields (agentic / normal mode).

    Raises:
        Exception: if the WebSocket connection is closed or broken.
    """
    try:
        # Calculate speed if not already set
        calculated_speed = stats.calculated_speed
        
        # Fallback: calculate from generated_tokens and total_duration
        if calculated_speed is None and stats.generated_tokens > 0 and stats.total_duration > 0:
            calculated_speed = round(stats.generated_tokens / stats.total_duration, 1)
        
        # Ensure tokens_remaining is calculated correctly
        tokens_remaining = stats.tokens_remaining
        if tokens_remaining < 0:
            tokens_remaining = 0
        
        # Build the message payload
        payload = {
            "type": "token_stats",
            "prompt_tokens": stats.prompt_tokens,
            "generated_tokens": stats.generated_tokens,
            "generation_speed": calculated_speed,
            "total_duration": round(stats.total_duration, 2),
            "provider_name": stats.provider_name,
            "model": stats.model,
            "context_window": stats.context_window,
            "tokens_remaining": tokens_remaining,
        }
        
        
        # Add per-turn session stats if provided (present in BOTH modes):
        #   session_generated_tokens - cumulative provider output for this whole turn
        #                              (agentic: summed over all loops; normal: the one call)
        if session_stats:
            for _key in ("session_generated_tokens",):
                if session_stats.get(_key) is not None:
                    payload[_key] = session_stats[_key]
        # Send to WebSocket
        await websocket.send_text(json.dumps(payload))
        
        logger.debug(
            f"Token stats sent: prompt={stats.prompt_tokens}, "
            f"generated={stats.generated_tokens}, "
            f"speed={calculated_speed} tok/s, "
            f"remaining={tokens_remaining}, "
            f"window={stats.context_window}"
        )
    except Exception as e:
        logger.warning(f"Failed to send token stats via WebSocket: {e}")
