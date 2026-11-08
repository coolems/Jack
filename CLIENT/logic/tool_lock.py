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
#
# 2026-11-07: the pyautogui mouse_interact/ screen_capture tools were replaced by the
# Computer Use port (tools/computer_use/*), so the screen tools are the cua_* set.
# compare_mouse_position went with them - cua_click/cua_hover report cursor_after, so
# pixel-circle comparison is no longer needed.
SCREEN_BROWSER_TOOLS: frozenset[str] = frozenset({
    # --- computer_use (UI Automation + Win32 SendInput: the physical desktop) ---
    "cua_capabilities",
    "cua_click",
    "cua_drag",
    "cua_get_app",
    "cua_get_state",
    "cua_hover",
    "cua_list_apps",
    "cua_list_windows",
    "cua_paste",
    "cua_perform_action",
    "cua_press_key",
    "cua_screenshot",
    "cua_scroll",
    "cua_select_text",
    "cua_show_window",
    "cua_type_text",
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
    # --- visual_browser_navigation: REMOVED 2026-11-08 (user request) ---
    # find_and_move_to_target and validate_target_area are gone: the Computer Use tools
    # (cua_get_state + cua_click, or window px from cua_screenshot) target elements
    # directly, so the banner-annotation bridge is dead weight in the tool schema. Their
    # files under tools/visual_browser_navigation/ are deleted, so neither name may stay
    # in this set - a name with no tool behind it only costs schema tokens.
})


#: Computer Use tools that need the LOCAL USER's per-app consent card (kind 'desktop').
#: cua_capabilities is deliberately NOT here: it is read-only preflight that opens no
#: dialog and grants nothing. The gate lives in logic/tool_executor.py; the tool files
#: re-verify the grant themselves (fail-closed) so a bypass cannot work.
DESKTOP_CONSENT_TOOLS: frozenset[str] = frozenset({
    "cua_get_app",          # binds an app handle - the call the user consents to
    "cua_list_apps",        # reads window titles of every running app
    "cua_list_windows",
    "cua_get_state",        # reads the app's UIA control tree
    "cua_screenshot",       # captures the app's window pixels
    "cua_click",
    "cua_hover",
    "cua_drag",
    "cua_scroll",
    "cua_type_text",
    "cua_press_key",
    "cua_paste",
    "cua_perform_action",
    "cua_select_text",
    "cua_show_window",
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

    # (2026-11 review fix B4) NO bare __aenter__/__aexit__ on this class.
    # The old pair returned ``self`` and did nothing on exit, so
    # ``async with tool_lock_instance:`` looked correct while running the tool
    # completely UNSERIALISED (and leaked no lock at all). Owner-scoped acquire/
    # release is the only correct protocol here, and it lives on _OwnerScopedLock
    # via the tool_lock(conv_id) factory. If someone ever writes
    # ``async with _SHARED_LOCK:`` they now get a clear TypeError instead of a
    # silent concurrency bug.
    async def __aenter__(self):  # pragma: no cover - guard, not a usage path
        raise TypeError(
            "ToolExecutionLock is not usable as a bare async context manager: it needs an "
            "owner. Use `async with tool_lock(conv_id):` (the _OwnerScopedLock wrapper) so "
            "acquire/release are paired and re-entrancy per conversation works."
        )

    async def __aexit__(self, *exc):  # pragma: no cover - paired with the guard above
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
