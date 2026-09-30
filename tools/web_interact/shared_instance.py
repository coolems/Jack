"""Shared WebInteract instance for remote tool execution.

When tools are compiled via exec() by RemoteToolOrchestrator, they cannot receive
a 'self' parameter. This module provides a global singleton that all web_interact
tools can import and use instead of 'self'.

CROSS-COPY ANCHOR (2026-09-14 reliability fix -- the 'goto: No page available' bug):
  The CLIENT loader re-executes this source once PER TOOL COMPILATION as a shared_dep
  (install_shared_deps), so a live client process can hold SEVERAL module copies of it,
  each with its own private '_instance'. connect() populated copy #1 while goto()'s
  bound functions closed over copy #2 -- which was empty -- and every tool that tried to
  auto-reconnect hit an unbound name (same-package imports are neutralized by the source
  transform). The fix: the real singleton lives in a process-wide anchor module stored
  under a fixed sys.modules key. Every exec'd copy of this file reads/writes that ONE
  anchor, so all tools -- whatever copy they closed over -- always see the same
  WebInteract instance and the same registered connect() function.
"""

import logging
import sys as _sys
import types as _types

logger = logging.getLogger("COOLEMS.Tools.WebInteract")

# Module-level alias (kept for backward compatibility with code that reads it directly).
_instance = None

_ANCHOR_KEY = "_coolems_web_interact_anchor"


def _ensure_anchor():
    """Return the process-wide anchor module (created once, shared by every copy)."""
    m = _sys.modules.get(_ANCHOR_KEY)
    if m is None:
        m = _types.ModuleType(_ANCHOR_KEY)
        m.instance = None      # the ONE WebInteract instance for this process
        m.connect_fn = None    # the compiled connect() tool, registered at its exec time
        _sys.modules[_ANCHOR_KEY] = m
    return m


# Runs once per exec'd copy; idempotent -- later copies reuse the same anchor.
_anchor = _ensure_anchor()


def get_web_interact():
    """Get or create the global WebInteract instance (anchor-backed, 2026-09-14).

    Returns:
        WebInteract instance, or None if not available.
    """
    global _instance

    # Fast path 1: this copy already holds it.
    if _instance is not None:
        return _instance

    # Fast path 2: another exec'd copy of this module created it (the common case on
    # the CLIENT, where each tool compile re-executes this source).
    if getattr(_anchor, "instance", None) is not None:
        _instance = _anchor.instance
        return _instance

    try:
        # Absolute form on purpose: after the CLIENT source transform it becomes
        # 'WI = web_interact.WebInteract', and the sibling module object named
        # 'web_interact' is injected into EVERY exec context of this file (both the
        # shared-module install and the per-tool dep installs). A relative import here
        # would be neutralized to nothing and leave 'WebInteract' unbound in some copies.
        from tools.web_interact import WebInteract as WI
        _instance = WI()  # __init__ calls set_web_interact -> updates the anchor too
        try:
            _ensure_anchor().instance = _instance  # explicit anchor write: never rely on a side effect alone
        except Exception:
            pass
        logger.info("shared_instance: Created new WebInteract singleton")
    except Exception as e:
        logger.warning(f"shared_instance: Could not create WebInteract instance: {e}")
        return None

    return _instance


def set_web_interact(instance):
    """Set the global WebInteract instance (used by __init__.py discovery)."""
    global _instance
    _instance = instance
    try:
        _ensure_anchor().instance = instance
    except Exception:
        pass
    logger.info("shared_instance: WebInteract singleton set externally")


def reset_web_interact():
        """Reset the global instance (for cleanup/reconnection).

        2026-08-29 threadless refactor: this used to fiddle with event loops
        (get_event_loop/close) which was both pointless and unsafe. It now only
        drops the reference -- callers that want a clean browser teardown should
        `await instance.disconnect()` BEFORE resetting.
        2026-09-14: also clears the anchor so no copy can resurrect a stale instance.
        """
        global _instance
        if _instance is not None:
            # Best-effort release of playwright state without blocking; real
            # disconnection is an async operation the caller must await first.
            _instance = None
        try:
            _ensure_anchor().instance = None
        except Exception:
            pass
        logger.info("shared_instance: WebInteract singleton reset")


def set_connect_function(fn):
    """Register the compiled connect() tool so sibling tools can trigger (re)connects.

    Called at module level by connect.py right after it is exec'd on the CLIENT.
    Same-package imports are neutralized by the source transform, so ensure_connected /
    ensure_page CANNOT import connect directly -- they resolve it through this anchor
    instead (2026-09-14 fix for the unbound 'do_connect' NameError path).
    """
    try:
        _ensure_anchor().connect_fn = fn
    except Exception as e:
        logger.warning(f"shared_instance: could not register connect function: {e}")


async def do_connect_fallback():
    """Call the registered connect() (if any) and return its result string.

    Used by ensure_connected()/ensure_page() when no live page exists. Returns a
    'Failed to connect...' message instead of raising when connect was never loaded in
    this session -- callers treat any non-success as False/None gracefully.
    """
    import asyncio as _aio

    fn = getattr(_anchor, "connect_fn", None)
    if fn is None:
        return "Failed to connect: connect function not available yet (call the connect tool first)"
    try:
        result = fn()
        if _aio.iscoroutine(result):
            result = await result
        return str(result)
    except Exception as e:
        logger.warning(f"shared_instance: do_connect_fallback raised: {e}")
        return f"Failed to connect: {e}"


async def _acquire_live_page(wi):
    """Best effort: return a LIVE Page from *wi*'s current state, or None.

    Tries in order (2026-09-14 reliability fix -- the old version gave up after a
    single failed step and returned None even when a perfectly good context existed):
      1. wi.page still alive            -> fast path (title check)
      2. any other open tab in the same browser/context is alive -> re-acquire it
      3. create a fresh page in the existing context (up to 3 tries with a short
         settle wait -- right after a CDP attach the first new_page() can race the
         target registration)

    A None return means "no usable page from THIS state" -- the caller decides
    whether a full reconnect is warranted. Never raises, never closes anything.
    """
    import asyncio as _aio

    # 1. fast path: existing page still alive
    if wi.page is not None:
        try:
            await wi.page.title()
            return wi.page
        except Exception:
            pass  # page died (crash/navigation) - fall through

    if wi.context is None or getattr(wi, "browser", None) is None:
        return None

    # 2. re-acquire from existing context pages
    try:
        for p in list(wi.context.pages):
            try:
                await p.title()
                wi.page = p
                return p
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"ensure_page: context.pages scan failed: {e}")

    # 3. create a fresh page in the existing context (retried)
    for _attempt in range(3):
        try:
            wi.page = await wi.context.new_page()
            await wi.page.title()
            return wi.page
        except Exception as e:
            logger.warning(f"ensure_page: could not create new page (attempt {_attempt + 1}/3): {e}")
            await _aio.sleep(0.5)

    return None


async def ensure_page():
        """Ensure a usable Playwright Page exists and return it.

        Handles every known edge case:
          * wi.page already valid           -> fast path (title check)
          * wi.page is None but context has open pages -> re-acquire first live page
          * browser/context gone            -> full reconnect via the registered connect()

        Returns the live Page object, or None if no browser could be reached.
        Callers should use the returned page (or wi.page afterwards).
        2026-09-10 fix: tools previously hard-failed with 'No page available' when
        wi.page was None even though a perfectly good context existed.
        2026-09-14 fix: the instance is anchor-backed (all tool copies share ONE), and
        the reconnect goes through do_connect_fallback() -- the old inline
        'from .connect import connect' was neutralized by the CLIENT source transform
        and raised NameError at runtime, so this path could never actually reconnect.
        """
        wi = get_web_interact()
        if wi is None:
            return None

        p = await _acquire_live_page(wi)
        if p is not None:
            return p

        # browser/context gone -> full reconnect (connect probes liveness + resets stale state)
        result = await do_connect_fallback()
        if "Failed to connect" in str(result):
            logger.warning(f"ensure_page: reconnect failed: {result}")
            return None

        wi = get_web_interact()
        if wi is None:
            return None
        return await _acquire_live_page(wi)
