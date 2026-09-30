"""Persistent plan log prompt - SERVER-delivered placeholder (CLIENT memory only).

    This file is intentionally EMPTY on the CLIENT side. The real content lives in
    the SERVER's tools/agent_prompt/persistent_plan_prompt.py and reaches the client
    as part of system_prompt via WebSocket during initialization (memory only, never
    written to disk here). Kept as a stable import target for backward compatibility.
    """

    # ===== PERSISTENT PLAN LOG PROMPT =====
PERSISTENT_PLAN_PROMPT = """

"""
