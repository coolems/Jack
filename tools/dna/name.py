"""Agent name - single source of truth"""

AGENT_NAME = "Jack"

def get_name() -> str:
    return AGENT_NAME

def set_name(new_name: str) -> None:
    global AGENT_NAME
    AGENT_NAME = new_name