"""Planning prompt - SERVER-delivered placeholder (CLIENT memory only).

This file is intentionally EMPTY on CLIENT side.
The real PLANNING_PROMPT content lives in SERVER's tools/agent_prompt/planning_prompt.py
and is delivered as part of system_prompt via WebSocket during initialization, stored in memory only.
"""

# Empty placeholder — populated at runtime from SERVER as part of system_prompt
PLANNING_PROMPT: str = ""
