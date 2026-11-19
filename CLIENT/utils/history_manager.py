"""History management.

Provides:
- Token-aware sliding window trimming
- Image base64 replacement with file-path references
- Combined load-and-prepare function for the WebSocket handler

Notes:
- estimate_message_tokens() handles base64 in tool results and caches its result on the
  message dict, so a message's length is computed once per loop pass. Tool-call messages
  cost 0 tokens while their arguments still consume real context.
- invalidate_message_tokens() drops that cache after in-place content mutations.
- sanitize_images_in_history() also cleans base64 embedded in string content.
"""

import json
import logging
from typing import List, Dict, Optional

from client_config import (
    max_history_tokens,
    estimate_tokens,
    DB_PATH,
    SYSTEM_PROMPT_TOKENS_ESTIMATE,
    IMAGE_TOKENS_PER_512PX,
)
from app.db_manager import get_db_connection

logger = logging.getLogger("COOLEMS.History")

# ---- Core functions ------

def _sanitize_base64_in_string(content: str) -> str:
        """Remove base64 image data from string content and replace with lightweight reference.

                Defense-in-depth: catches any base64 that slipped through tool_executor sanitization.

                CRITICAL: Python str(dict) uses SINGLE quotes ('), not double quotes (").
                The regex must match BOTH formats since tool results may come as either.

                The whole key:value pair (through the closing quote) is consumed, mirroring
                tool_executor._sanitize_image_result: replacing only the opening-quote span
                would leave the base64 value behind and make the result LONGER than the input.

                Args:
                    content: String content (may contain base64 from tool results).

                Returns:
                    Sanitized string with base64 replaced by references.


        """
        import re as _re

        BASE64_THRESHOLD = 10000

        # Match BOTH Python dict single quotes AND JSON double quotes:
        #   'image_base64': '...'  (Python str(dict))
        #   "image_base64": "..."  (JSON)
        pattern = _re.compile(r"""['"](\w*base64\w*)['"]\s*:\s*(["'])""")

        # Filename hint for the reference - search once, not per match.
        filename = "image"
        path_match = _re.search(
            r"""['"](?:screenshot_path|filepath|path)['"]\s*:\s*['"]([^'"]+)['"]""", content
        )
        if path_match:
            import os as _os
            filename = _os.path.basename(path_match.group(1))

        parts = []
        last_end = 0

        for match in pattern.finditer(content):
            key = match.group(1)
            quote_char = match.group(2)  # ' or " -- the quote used for values

            # Find the closing quote of the base64 value (handle escaped quotes)
            value_start = match.end()  # first char AFTER opening quote
            pos = value_start
            while pos < len(content):
                if content[pos] == '\\' and pos + 1 < len(content):
                    pos += 2
                    continue
                if content[pos] == quote_char:
                    break
                pos += 1

            base64_value = content[value_start:pos]

            if len(base64_value) >= BASE64_THRESHOLD:
                # Consume the ENTIRE pair: from match start through closing quote.
                parts.append(content[last_end:match.start()])
                replacement = (
                    f"{quote_char}{key}{quote_char}: "
                    f"{quote_char}[image: {filename} ({len(base64_value):,} chars sanitized)]{quote_char}"
                )
                parts.append(replacement)
                last_end = pos + 1

        if not parts:
            return content  # No replacements made

        parts.append(content[last_end:])  # Everything after last replacement
        return ''.join(parts)


    
def estimate_message_tokens(msg: dict) -> int:
    """Estimate the token cost of one message dict (CACHED on the dict).

    Prefers an EXACT server-tokenizer count (`_exact_tokens`, filled by
    utils.token_counter) over the chars/4 estimate (`_est_tokens`). Images are replaced
    with a short path reference before counting, so a screenshot costs ~10 tokens instead
    of its base64 length.
    """
    if isinstance(msg, dict):
        exact = msg.get("_exact_tokens")
        if isinstance(exact, int) and exact >= 0:
            return exact
        cached = msg.get("_est_tokens")
        if isinstance(cached, int) and cached >= 0:
            return cached

    tokens = 0
    # role token overhead
    tokens += 3

    # content - handle both string and list formats
    if isinstance(msg.get("content"), str):
        content = msg["content"]

        # CRITICAL FIX: Sanitize base64 data before counting tokens.
        # Tool results may contain massive base64 strings that should NOT
        # be counted as real token cost (they get sanitized anyway).
        if 'base64' in content.lower() and len(content) > 10000:
            content = _sanitize_base64_in_string(content)

        tokens += estimate_tokens(content)
    elif isinstance(msg.get("content"), list):
        for part in msg["content"]:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    tokens += estimate_tokens(part.get("text", ""))
                elif part.get("type") == "image_url":
                    # Image URLs are counted as fixed token cost (vision model)
                    tokens += IMAGE_TOKENS_PER_512PX
            else:
                tokens += estimate_tokens(str(part))

    # tool_calls — assistant messages that asked for tools. Their arguments
    # (e.g. full python_exec code blocks) are real prompt content and MUST be
    # counted, or the trim math is blind to the fastest-growing part of history.
    tcs = msg.get("tool_calls") if isinstance(msg, dict) else None
    if tcs:
        try:
            tokens += estimate_tokens(json.dumps(tcs))
        except (TypeError, ValueError):
            tokens += estimate_tokens(str(tcs))

    # images key - base64 blobs stored separately from content
    images = msg.get("images", [])
    if images:
        tokens += len(images) * IMAGE_TOKENS_PER_512PX

    if isinstance(msg, dict):
        try:
            msg["_est_tokens"] = tokens  # cache for all future passes
        except Exception:
            pass  # immutable dict or similar — counting still works, just uncached

    return tokens


def invalidate_message_tokens(msg: dict) -> None:
    """Drop the cached `_est_tokens` of a message whose content was mutated in place.

    Use this after appending to msg["content"] (e.g. merged thinking steps) so
    the next get_token_count()/trim pass recomputes instead of using the stale
    cache. No-op for messages that were never counted yet.
    """
    if isinstance(msg, dict):
        msg.pop("_est_tokens", None)
        msg.pop("_exact_tokens", None)


def trim_history_to_token_limit(
    messages: List[Dict],
    max_tokens: Optional[int] = None,
    reserve_for_system: int = SYSTEM_PROMPT_TOKENS_ESTIMATE,
) -> List[Dict]:
    """Return a sub-list of *messages* that fits within *max_tokens* tokens.
    The most-recent messages are kept; the oldest are dropped.

    There is NO minimum-message floor: if the budget requires it,
    every message may be dropped and only system prompt + current user prompt
    remain - same contract as safe_trim_history() in utils/history_trimmer.py.
    """
    if max_tokens is None:
        max_tokens = max_history_tokens()

    effective_budget = max_tokens - reserve_for_system
    if effective_budget <= 0:
        effective_budget = max_tokens

    # Quick path - already fits
    total = sum(estimate_message_tokens(m) for m in messages)
    if total <= effective_budget:
        return messages

    # Trim oldest first until the token budget fits (no minimum-message floor).
    trimmed = list(messages)
    while sum(estimate_message_tokens(m) for m in trimmed) > effective_budget:
        trimmed.pop(0)

    logger.info(
        f"History trimmed: {len(messages)} -> {len(trimmed)} messages "
        f"({sum(estimate_message_tokens(m) for m in trimmed)} tokens)"
    )
    return trimmed


def sanitize_images_in_history(messages: List[Dict]) -> List[Dict]:
    """Replace any base64 image data in messages with a lightweight reference
    string like "[image: filename.png]".

    This prevents the history list from ballooning with megabytes of base64.
    The actual image files remain on disk and can be re-loaded when needed.

    FIXED: Also sanitizes base64 data embedded in string content
    (from tool results) not just the separate 'images' key.
    """
    cleaned = []
    for msg in messages:
        new_msg = dict(msg)

        # Strip images key (base64 blobs stored separately)
        if "images" in new_msg and new_msg["images"]:
            # Try to guess filenames from paths if available
            paths = new_msg.get("_image_paths", [])
            if paths:
                refs = ", ".join(f"[image: {p}]" for p in paths)
            else:
                refs = f"[{len(new_msg['images'])} image(s) attached]"
            # Append reference to content
            content = new_msg.get("content", "")
            if content:
                new_msg["content"] = f"{content}\n{refs}"
            else:
                new_msg["content"] = refs
            del new_msg["images"]

        # CRITICAL FIX: Also sanitize base64 data in string content (from tool results)
        if isinstance(new_msg.get("content"), str):
            original_content = new_msg["content"]
            sanitized_content = _sanitize_base64_in_string(original_content)

            if len(sanitized_content) < len(original_content):
                saved_chars = len(original_content) - len(sanitized_content)
                logger.info(
                    f"[SANITIZE] Cleaned base64 from message: "
                    f"{len(original_content):,} -> {len(sanitized_content):,} chars "
                    f"({saved_chars:,} chars saved)"
                )
                new_msg["content"] = sanitized_content

        cleaned.append(new_msg)
    return cleaned


def count_messages_for_conv(
    conv_id: str,
    db_path: str = None,
) -> int:
    """Lightweight COUNT query for a conversation. Returns message count without loading content.
    Used by the WebSocket handler to detect history changes without full DB scan.
    """
    if db_path is None:
        db_path = DB_PATH

    # (2026-08-21 fix) the query block was spliced INSIDE the `if db_path is None:`
    # branch, so any call passing an explicit db_path silently returned None.
    # Dedented to run unconditionally — matches load_conversation_history() below.
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (conv_id,),
        )
        count = cursor.fetchone()[0]
        return count
    finally:
        conn.close()


def load_conversation_history(
    conv_id: str,
    max_tokens: Optional[int] = None,
    include_images: bool = False,
    db_path: str = None,
) -> List[Dict]:
    """Load messages from the DB for *conv_id*, apply token-aware trimming,
    and optionally strip image base64 data.

    Returns a list of dicts suitable for sending to Ollama.
    """
    if db_path is None:
        db_path = DB_PATH

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content, media_urls, file_contents "
            "FROM messages WHERE conversation_id = ? ORDER BY timestamp",
            (conv_id,),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    messages: List[Dict] = []
    for row in rows:
        role, content, media_urls, file_contents = row
        msg: Dict = {"role": role, "content": content or ""}

        # Re-attach image paths (stored as media_urls JSON) so the
        # agentic / normal mode can re-load them when *needed*.
        if media_urls:
            try:
                paths = json.loads(media_urls)
                if paths:
                    msg["_image_paths"] = paths
            except (json.JSONDecodeError, TypeError):
                logger.debug("Parse/decode error ignored at CLIENT/utils/history_manager.py:191")

        # Re-attach file contents reference
        if file_contents:
            try:
                fc = json.loads(file_contents)
                if fc:
                    msg["_file_contents"] = fc
            except (json.JSONDecodeError, TypeError):
                logger.debug("Parse/decode error ignored at CLIENT/utils/history_manager.py:200")

        messages.append(msg)

    # Token-aware trim
    messages = trim_history_to_token_limit(messages, max_tokens=max_tokens)

    # Strip base64 images from history (keep only references)
    if not include_images:
        messages = sanitize_images_in_history(messages)

    return messages
