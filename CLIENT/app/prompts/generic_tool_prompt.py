"""Generic tool prompt - SERVER-delivered placeholder (CLIENT memory only).

This file is intentionally EMPTY on CLIENT side.
The real GENERIC_TOOL_PROMPT content lives in SERVER's tools/agent_prompt/generic_tool_prompt.py
and is delivered via WebSocket during initialization, stored in memory only.

Imported code uses `from app.prompts import GENERIC_TOOL_PROMPT` which gets the
runtime-updated value from __init__.py after SERVER delivery.
"""

# Empty placeholder — populated at runtime from SERVER
GENERIC_TOOL_PROMPT: str = ""
