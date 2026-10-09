"""
Agentic chat mode logic - AI with tool orchestration and ReAct loop.

Thin wrapper that imports from focused modules:
- tool_executor.py: ToolExecutor class
- react_loop.py: Main ReAct loop control flow  
- provider_call.py: Provider call with automatic restart
"""

import json
import logging
from typing import List, Dict, Any
from dataclasses import dataclass, field

from .base_mode import BaseModeConfig, build_messages, push_token_stats, check_stop_event
from .tool_executor import ToolExecutor
from .react_loop import run_react_loop


logger = logging.getLogger("COOLEMS.Logic.Agentic")


# Lazy-import ToolCompilationError to avoid circular dependency at module load time
def _get_tool_compilation_error():
    """Get the ToolCompilationError class (lazy import)."""
    try:
        from tools.remote_tools.dynamic_loader import ToolCompilationError
        return ToolCompilationError
    except ImportError:
        # Fallback for when running in CLIENT context where module path differs
        try:
            from CLIENT.tools.remote_tools.dynamic_loader import ToolCompilationError
            return ToolCompilationError
        except ImportError:
            return None


@dataclass
class AgenticModeConfig(BaseModeConfig):
    """Configuration for agentic chat mode — inherits shared fields from BaseModeConfig"""
    generic_tool_prompt: str = ""

    # Tool execution (agentic-only)
    tool_orchestrator: Any = None
    ollama_tools: List[Dict] = field(default_factory=list)
    
    # API key for role-based permission checking
    api_key: str = None


async def agentic_mode(
    config: AgenticModeConfig,
    conversation_history: List[Dict],
    enhanced_message: str,
    media_files: List[str] = None
) -> str:
    """
    Run agentic chat mode with tool orchestration and ReAct loop.
    
    HISTORY MANAGEMENT:
    - Full conversation history is preserved during the entire task.
    - ONLY when the token limit is exceeded, oldest ATOMIC UNITS are removed.
      A unit = a user turn + the AI reply that answered it (assistant text and/or
      tool_calls) + ALL its tool results. Units are never split, so an assistant
      tool_calls message can never be orphaned from its tool results.
    - The system prompt is NEVER removed.
    - The CURRENT USER PROMPT is NEVER removed (tagged `_protect` by build_messages,
      passed to the loop as a protected id) - it must remain at all times.
    - No minimum-message floor: in a long run only [system + current user prompt]
      may remain, and that is fine.
    - At 95% of the safe token budget (AGENTIC_TOKEN_WARNING_THRESHOLD) a console
      warning fires once per run and a context-guardian [SYSTEM NOTICE] tells the AI to
      finish its current work step, write a plan_<...>_continue.md file with what is done
      / remaining so we can resume in the next work loop, and end with TASK DONE.
    - Per-message token lengths are cached on the message dicts (`_est_tokens`)
      so trimming never re-tokenizes history every iteration.

    ROLE-BASED PERMISSIONS:
    - Passes API key to ToolExecutor for permission checking
    - Tools not allowed by the user's role will be blocked
    
    TOKEN STATISTICS:
    - Token stats are sent to UI after EVERY provider call
    - This includes both intermediate tool calls AND final responses
    - The UI updates the status bar with real-time token usage
    """
    
    logger.info("="*60)

    # Build agent system prompt
    agent_system = f"""
{config.dna_content}

{config.generic_tool_prompt}
"""

    # Add image context if images are present (2026-08-23 fix: VISION-FIRST policy).
    # The uploaded pixels ride on THIS message as base64 (see build_messages below),
    # so the model can SEE them directly. transcribe_image is ONLY for explicit OCR asks -
    # it must never be the default reaction to an attached image.
    if config.image_data and config.image_paths:
        image_context = """
IMPORTANT - Images attached to this message (VISION-FIRST):
The actual pixels of exactly these images are included as base64 with this very message, so you can SEE them directly:
""" + "\n".join([f"- {path}" for path in config.image_paths]) + """

RULES:
1. Answer from what you see. Do NOT call any tool just to look at an attached image - the pixels are already here with this message.
2. Only the files listed above exist for this message; never invent other image filenames.
3. Use transcribe_image (OCR) ONLY when the user explicitly asks to transcribe / extract text from these images ("transcribe", "extract the text", "what does it say"). Never run OCR by default just because an image is attached - seeing the image is not the same as transcription.
4. If you genuinely cannot see the pixels (vision reported unavailable for this model) and the user\'s question depends on the image content, transcribe_image is allowed as a last resort - call it on one of the exact filenames listed above."""
        agent_system += image_context


    # (2026-08-26) Per-turn attached-image registry: hand the CURRENT turn's uploaded
    # pixels to the delivered tools.utils module so transcribe_image() can use them
    # DIRECTLY as base64 (same bytes that ride on this message for vision) without a
    # disk round-trip. Cleared in the finally block below when the turn ends.
    try:
        from tools.utils import set_current_turn_attachments, get_current_turn_attachments
        if config.image_data and config.image_paths:
            set_current_turn_attachments(config.image_data, config.image_paths)
            logger.info(
                f"[AGENTIC.DEBUG] Registered {len(get_current_turn_attachments())} attached image(s) "
                "for direct base64 transcription this turn"
            )
        else:
            set_current_turn_attachments()  # clear any stale registration from a previous turn
    except Exception as e:
        logger.debug(f"[AGENTIC.DEBUG] Turn-attachment registry unavailable (non-fatal): {e}")

    # Prepare messages for agent loop using shared helper
    messages = build_messages(
        system_prompt=agent_system,
        conversation_history=conversation_history,
        user_message_content=enhanced_message,
        image_data=config.image_data,
    )

    # Initialize tool executor with provider and API key
    logger.debug("[AGENTIC.DEBUG] agentic_mode() - Initializing ToolExecutor")
    
    # DEBUG: Trace what we are passing to ToolExecutor
    logger.debug(f"[AGENTIC.DEBUG] config.tool_orchestrator = {config.tool_orchestrator} (type={type(config.tool_orchestrator).__name__})")
    if config.tool_orchestrator is None:
        logger.error("[AGENTIC.DEBUG] CRITICAL BUG: tool_orchestrator is None! Will cause execute_tool error.")
    
    tool_executor = ToolExecutor(
            config.tool_orchestrator,
            None,
            provider=config.provider,
            current_model=config.model,
            api_key=config.api_key,
            session_timer=getattr(config, "session_timer", None),  # (2026-08-23) pause clock while user menus are open
        )

    # (2026-10-09) Per-workspace working root: resolve THIS conversation's folder from
    # the database and publish it to tools.utils for the whole turn. Tools read the
    # ContextVar (set_current_working_root), so concurrent workspaces each execute
    # against their OWN folder - no shared file, no cross-workspace bleed. Workspaces
    # without a stored value fall back to the project root. Cleared in the finally
    # block below when the turn ends.
    try:
        from app.utils.common import resolve_working_root_for_conv
        from tools.utils import set_current_working_root as _set_wr_ctx
        _turn_wr = resolve_working_root_for_conv(config.conversation_id)
        _set_wr_ctx(_turn_wr)
        logger.info(f"[AGENTIC.DEBUG] Working root for this turn (conv={str(config.conversation_id)[:8]}): {_turn_wr}")
    except Exception as e:
        logger.warning(f"[AGENTIC.DEBUG] Per-turn working root resolution failed (tools use fallback): {e}")

    # Run the ReAct loop
    try:
        return await run_react_loop(
            config, messages, tool_executor, conversation_history,
        )
    except Exception as e:
        # CRITICAL FIX: Check for ToolCompilationError BEFORE catching generic RuntimeError.
        # ToolCompilationError inherits from RuntimeError and was being caught by the 
        # (ConnectionError, TimeoutError, RuntimeError, FileNotFoundError) tuple below,
        # which caused it to be logged as "PROVIDER ERROR" - completely hiding the real
        # compilation failure from the user and triggering useless retries.
        tool_comp_error = _get_tool_compilation_error()
        if tool_comp_error and isinstance(e, tool_comp_error):
            logger.error(f"[AGENTIC.DEBUG] agentic_mode() - TOOL COMPILATION FAILED (non-retryable): {e}")
            raise  # Re-raise immediately — do NOT retry compilation errors
        
        # Original provider error handling for connection/network issues only
        if isinstance(e, (ConnectionError, TimeoutError, FileNotFoundError)):
            logger.error("[AGENTIC.DEBUG] agentic_mode() - PROVIDER ERROR")
            raise
        # All other unexpected errors
        logger.error(f"[AGENTIC.DEBUG] agentic_mode() - UNEXPECTED ERROR: {type(e).__name__}: {e}")
        raise
    finally:
        # (2026-10-09) Clear the per-turn working root so a stale folder can never leak
        # into the NEXT turn's tool execution.
        try:
            from tools.utils import set_current_working_root as _clear_wr_ctx
            _clear_wr_ctx(None)
        except Exception:
            pass
        # (2026-08-26) Clear the per-turn attachment registry so a stale image can
        # never leak into the NEXT turn's transcription.
        try:
            from tools.utils import set_current_turn_attachments as _clear_turn_attachments
            _clear_turn_attachments()  # empty args = clear the current-turn registry
        except Exception:
            pass
