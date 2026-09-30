"""ToolExecutionLock — serializes screen/browser tools across concurrent agentic chats.

2026-09-08 multi-chat (Phase 5). When multiple chats generate simultaneously, their
agentic loops may both want to use mouse/keyboard/screenshot tools at the same time.
The physical screen and browser are SINGLETON resources — two tool calls fighting over
the cursor produce garbage screenshots and broken automation.

Design:
  * One module-level asyncio.Lock shared by ALL ToolExecutor instances in this process.
  * Only tools in SCREEN_BROWSER_TOOLS acquire the lock; file/HTTP/search/vision tools
    run in parallel (they don't touch the screen).
  * Owner tag = conversation ID, so logs show WHICH chat is holding the lock and how
    long others wait.
  * The lock is re-entrant per owner: if the same chat calls two browser tools back-to-
    back it doesn't deadlock on itself (it already holds the slot).

Usage in ToolExecutor.execute():
    from .tool_lock import tool_lock, SCREEN_BROWSER_TOOLS
    if tool_name in SCREEN_BROWSER_TOOLS:
        async with tool_lock(conv_id):
            ... execute ...
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("COOLEMS.Logic.ToolLock")


# Tools that physically interact with the screen, mouse, or shared browser instance.
# Anything NOT in this set runs without the lock (file ops, HTTP fetches, searches, etc.).
SCREEN_BROWSER_TOOLS: frozenset[str] = frozenset({
    # --- mouse_interact ---
    "click_left",
    "click_middle",
    "click_right",
    "drag",
    "get_pos",
    "get_screen_size",
    "scroll_down",
    "scroll_up",
    "set_pos",
    "set_pos_relative",
    # --- screen_capture ---
    "full_desktop_screenshot",
    # --- web_interact (shared browser) ---
    "browser_screenshot",
    "check_url",
    "connect",
    "disconnect",
    "download_file",
    "ensure_connected",
    "get_page_content",
    "go_back",
    "goto",
    "reload",
    "save_current_image",
    "scroll",
    # --- visual_browser_navigation ---
    "compare_mouse_position",
    "find_and_move_to_target",
    "validate_target_area",
})


class ToolExecutionLock:
    """Asyncio lock with owner tracking for screen/browser tool serialization.

    Re-entrant per owner: if the same conversation already holds the lock, a second
    acquire from that owner is a no-op (it's its own turn). A DIFFERENT owner must wait.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner: Optional[str] = None
        self._acquired_at: float = 0.0
        self._wait_log: list[tuple[float, str]] = []  # (timestamp, owner) for observability
        self._hold_count: int = 0

    @property
    def current_owner(self) -> Optional[str]:
        return self._owner

    async def acquire(self, owner: str) -> None:
        """Acquire the lock for *owner*. No-op if *owner* already holds it."""
        if self._owner == owner:
            self._hold_count += 1
            return  # re-entrant: same chat calling a second screen/browser tool
        wait_start = time.monotonic()
        await self._lock.acquire()
        waited = time.monotonic() - wait_start
        self._owner = owner
        self._acquired_at = time.monotonic()
        if waited > 0.5:
            logger.info(
                "[TOOL_LOCK] Chat %s acquired screen/browser lock after %.1fs wait "
                "(previous holder: %s)",
                owner, waited, self._wait_log[-1][1] if self._wait_log else "?",
            )
        else:
            logger.debug("[TOOL_LOCK] Chat %s acquired (no significant wait)", owner)

    def release(self, owner: str) -> None:
        if self._owner != owner:
            logger.warning("[TOOL_LOCK] Release by non-owner %s (current: %s) — ignored",
                           owner, self._owner)
            return
        self._hold_count -= 1
        if self._hold_count > 0:
            logger.debug("[TOOL_LOCK] Chat %s inner release (still held x%d)", owner, self._hold_count)
            return
        held = time.monotonic() - self._acquired_at
        self._wait_log.append((time.monotonic(), owner))
        # keep log bounded
        if len(self._wait_log) > 50:
            self._wait_log = self._wait_log[-25:]
        self._owner = None
        self._hold_count = 0
        self._lock.release()
        logger.debug("[TOOL_LOCK] Chat %s released (held %.1fs)", owner, held)

    async def __aenter__(self):
        # The context manager is used as:  async with tool_lock(conv_id): ...
        # We store the owner on a thread-local-ish attribute set by the wrapper.
        return self

    async def __aexit__(self, *exc):
        pass


class _OwnerScopedLock:
    """Context-manager wrapper that binds an owner to acquire/release."""

    def __init__(self, lock: ToolExecutionLock, owner: str):
        self._lock = lock
        self._owner = owner

    async def __aenter__(self) -> None:
        await self._lock.acquire(self._owner)

    async def __aexit__(self, *exc) -> None:
        self._lock.release(self._owner)


def tool_lock(owner: str) -> _OwnerScopedLock:
    """Factory for the shared lock scoped to one conversation.

    Usage:
        async with tool_lock(conv_id):
            result = await orchestrator.execute_tool(...)
    """
    return _OwnerScopedLock(_SHARED_LOCK, owner)


# Module-level singleton — all ToolExecutor instances in this process share it.
_SHARED_LOCK = ToolExecutionLock()
