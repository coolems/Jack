"""
WebSocket /search command handler.

Handles the `/search <query>` command which triggers a web search,
optionally translates non-English queries, summarizes results, and
compares with a previous answer if provided.

UPDATED: Passes API key to tool orchestrator for role-based permission checking.
FIXED: Lazy import of call_ollama_non_streaming to break circular import chain.
"""

import logging
from typing import Dict, Any, Optional

from app.websocket.message_types import (
    send_tool_start,
    send_tool_end,
    send_content,
    send_done,
)
from app.websocket.db_ops import save_search_result

# FIXED: Removed top-level import of call_ollama_non_streaming from logic.normal
# to break circular import chain:
#   logic/__init__ → logic/normal → app.websocket.message_types
#     → app.websocket.__init__ → handler → search_handler → logic.normal (CIRCULAR!)
# Now imported lazily inside the functions that use it.

logger = logging.getLogger("COOLEMS.WebSocket.Search")

async def handle_search_command(
    user_msg: str,
    search_query: str,
    model: str,
    provider: Any,
    tool_orchestrator: Any,
    websocket: Any,
    agent: Any,
    db_path: str,
    conv_id: str,
    search_engines: Dict,
    conversation_history: list,
    api_key: str = None,  # NEW: API key for permission checking
):
    """
    Handle a /search command end-to-end.

    Args:
        user_msg: The original user message (e.g. "/search what is X")
        search_query: The extracted search query (without /search prefix)
        model: Model name to use for summarization
        provider: The AI provider instance
        tool_orchestrator: Tool orchestrator for executing web_search
        websocket: The WebSocket connection
        agent: The agent DNA instance
        db_path: Path to SQLite database
        conv_id: Conversation ID
        search_engines: Dict of search engine config
        conversation_history: Current conversation history list
        api_key: API key for role-based permission checking (NEW)

    Returns:
        True if handled successfully, False on error.
    """
    # Parse optional "Previous answer for comparison:" block
    previous_answer = None
    if "Previous answer for comparison:" in search_query:
        parts = search_query.split("Previous answer for comparison:")
        search_query = parts[0].strip()
        previous_answer = parts[1].strip() if len(parts) > 1 else None

    # Translate non-English queries to English
    if search_query and not search_query.isascii():
        search_query = await _translate_to_english(
            search_query, model, provider
        )

    # Execute web search with permission checking
    await send_tool_start(
        websocket,
        tool="web_search",
        input_data=str({"query": search_query})
    )

    # Execute tool with API key for permission checking
    # FIXED (2026-08-28): await the call -- CLIENT's orchestrator is always
    # RemoteToolOrchestrator whose execute_tool() is async. Without await this
    # returned an unawaited coroutine: the role-permission check never ran and
    # the model was fed the literal "<coroutine object ...>" text instead of results.
    web_results = await tool_orchestrator.execute_tool(
        "web_search",
        {"query": search_query, "max_results": 8, "search_engines": search_engines},
        conversation_history=conversation_history,
        api_key=api_key,  # NEW: Pass API key for role-based permission check
    )

    # Check if tool execution was blocked due to permissions
    if isinstance(web_results, str) and web_results.startswith("ERROR: Tool"):
        await send_tool_end(
            websocket,
            tool="web_search",
            output=web_results[:500],
        )
        await send_content(websocket, content=f"\n\n❌ {web_results}")
        await send_done(websocket)
        logger.warning("[SEARCH] Tool 'web_search' blocked due to role permissions")
        return False

    await send_tool_end(
        websocket,
        tool="web_search",
        output=str(web_results)[:2000],
    )

    # Summarize results
    web_summary = await _summarize_search_results(
        search_query, web_results, previous_answer,
        model, provider, agent,
    )

    await send_content(
        websocket,
        content="\n\nWeb Search Results:\n\n" + web_summary,
    )

    # Update conversation history
    conversation_history.append({"role": "user", "content": user_msg})
    conversation_history.append({"role": "assistant", "content": web_summary})

    # Save to DB
    save_search_result(db_path, conv_id, user_msg, web_summary)

    await send_done(websocket)
    return True

async def _translate_to_english(query: str, model: str, provider: Any) -> str:
    """Translate a non-English query to English for better search results."""
    # FIXED: Lazy import to break circular import
    from logic.normal import call_ollama_non_streaming

    try:
        translate_system = (
            "You are a translator. Translate the following text to English. "
            "Only output the translated text, nothing else."
        )
        translate_messages = [
            {"role": "system", "content": translate_system},
            {"role": "user", "content": f"Translate this to English: {query}"},
        ]
        english_query = await call_ollama_non_streaming(
            model, translate_messages, temperature=0.3, provider=provider
        )
        if english_query and english_query.strip():
            logger.info(f"[SEARCH] Translated query to English: {english_query}")
            return english_query.strip()
    except Exception as e:
        logger.warning(f"[SEARCH] Translation failed: {e}")
    return query

async def _summarize_search_results(
    query: str,
    web_results: str,
    previous_answer: Optional[str],
    model: str,
    provider: Any,
    agent: Any,
) -> str:
    """
    Summarize web search results, optionally comparing with a previous answer.
    """
    # FIXED: Lazy import to break circular import
    from logic.normal import call_ollama_non_streaming

    if previous_answer:
        summarize_system = f"""You are {agent.get_name()}. Below is information gathered from web search about the same question.

You also have the previous answer already received from local AI.

Compare the web search results to the previous answer. Point out any differences, corrections, or updates.

If the web results confirm the previous answer, say so. Format nicely with bullet points. Be concise."""

        summarize_messages = [
            {"role": "system", "content": summarize_system},
            {"role": "user", "content": (
                f"Question: {query}\n\n"
                f"Previous local AI answer:\n{previous_answer}\n\n"
                f"Web search results:\n{web_results}\n\n"
                f"Please compare and summarize."
            )},
        ]
    else:
        summarize_system = f"""You are {agent.get_name()}. Below is information gathered from web search.

Summarize the key findings. Format nicely with bullet points. Be concise."""

        summarize_messages = [
            {"role": "system", "content": summarize_system},
            {"role": "user", "content": (
                f"Question: {query}\n\n"
                f"Web search results:\n{web_results}\n\n"
                f"Please summarize."
            )},
        ]

    return await call_ollama_non_streaming(
        model, summarize_messages, temperature=0.7, provider=provider
    )
