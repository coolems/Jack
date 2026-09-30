"""Model/tool name filtering helpers shared by direct + relay WS paths.

Moved verbatim from ws_client_handler.py on 2026-09-07 (lines 494-554).
_tool_name_of is also imported by chat_relay so both paths filter identically.
"""

from .log import logger

def _tool_name_of(tool):
    """Extract the tool name from a tool schema entry (dict or plain string).

    Shared by chat_relay (direct + relay paths) so both filter identically.
    Returns '' when no name is determinable.
    """
    if isinstance(tool, dict):
        fn = tool.get("function", {})
        if isinstance(fn, dict) and "name" in fn:
            return str(fn["name"])
        if "name" in tool:
            return str(tool["name"])
    elif isinstance(tool, str):
        return tool
    return ""

def _normalize_model_entry(entry):
    """Normalize a model entry to dict format {"name": str, ...}.

    Handles both string and dict inputs WITHOUT double-wrapping.
    - String input "model.gguf" -> {"name": "model.gguf"}
    - Dict input {"name":"model.gguf","size":123} -> passed through unchanged
    """
    if isinstance(entry, str):
        return {"name": entry}
    elif isinstance(entry, dict):
        # Already a dict -- ensure it has a 'name' key that is a string
        name = entry.get("name", "")
        if not isinstance(name, str):
            logger.warning(f"[SERVER] Model dict has non-string name ({type(name).__name__}), using empty")
            entry = dict(entry)  # shallow copy to avoid mutating original
            entry["name"] = ""
        return entry
    else:
        logger.warning(f"[SERVER] Unexpected model entry type {type(entry).__name__}, skipping")
        return None


def _filter_models_by_user(models, user_info, server_provider):
    """Filter model list by user's allowed models.

    Handles mixed input formats (strings from health_check(), dicts from get_models_list()).
    Returns filtered list of dict entries in consistent format.

    CRITICAL: Must NOT double-wrap entries that are already dicts!
    - health_check() returns: ["model1.gguf", "model2.gguf"]  (list of strings)
    - get_models_list() returns: [{"name":"m1.gguf","size":...}, ...]  (list of dicts)
    """
    # Normalize all entries to dict format first (no double-wrapping!)
    normalized = []
    for m in models:
        entry = _normalize_model_entry(m)
        if entry is not None and entry.get("name"):
            normalized.append(entry)

    if not user_info:
        return normalized

    filtered = server_provider._auth_gateway.get_allowed_models(user_info, normalized)
    logger.info(f"[SERVER] Filtered {len(normalized)} models -> {len(filtered)} for role={user_info.get('role')}")
    return filtered
