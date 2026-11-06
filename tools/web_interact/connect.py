"""Connect function - connects to Chrome browser via CDP"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "connect",
        "description": "Connect to an already-running Chrome browser via CDP. ALWAYS call this FIRST before any browser automation. Auto-starts a fresh debug-mode Chrome window if none is reachable.",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}

import logging
import subprocess
import asyncio

logger = logging.getLogger("COOLEMS.Tools.WebInteract")

# Polling budgets for the auto-launch path. Chrome binds the debug port well under 1s
# on a warm machine, so we poll instead of sleeping a fixed 5/10s (2026-11-06 latency
# fix: the fixed sleep added 5s on every fresh launch even when the port was ready).
CDP_POLL_INTERVAL = 0.25
CDP_POLL_TIMEOUT = 15.0
PAGE_SETTLE = 0.25

# Only a genuinely unreachable endpoint may trigger a Chrome launch. The old heuristic
# ('connect' in msg) also matched Playwright's "Target page, context or browser has been
# closed" from a STOPPED driver - that spawned a second browser window while the first one
# already held the port (2026-11-06 incident).
_ENDPOINT_UNREACHABLE_MARKERS = (
    "econnrefused",
    "connection refused",
    "could not connect",
    "connectionrefused",
    "failed to connect",
    "socket hang up",
    "network error",
    "timed out",
)


def _is_endpoint_unreachable(message: str) -> bool:
    """True only when nothing answers the CDP port - never for driver/target lifecycle errors."""
    lowered = message.lower()
    return any(marker in lowered for marker in _ENDPOINT_UNREACHABLE_MARKERS)


async def _reset_wi_state(wi, stop_driver: bool = False) -> None:
    """Drop stale connection state from a WebInteract instance.

    2026-09-14 fix (goto 'No page available'): the old code set ``wi._connected = True``
    and never cleared it, so after the browser died the flag stayed True forever and every
    later connect() short-circuited with "Already connected" while holding a dead page.
    Any failure path must reset state so the NEXT call performs a real CDP handshake.

    2026-11-06 latency fix: by default the Playwright DRIVER stays alive. ``browser.close()``
    on a CDP-attached browser only releases OUR handle - it does not close any Chrome window
    (verified: 24 chrome.exe processes before and after, tabs intact) - and reusing one driver
    costs ~0.03s instead of ~1.7s for stop+start. ``stop_driver=True`` is used only when the
    driver itself must be torn down (disconnect / process cleanup).
    """
    if stop_driver:
        try:
            if getattr(wi, "playwright", None) is not None:
                await wi.playwright.stop()
        except Exception as stop_error:
            logger.debug(f"connect: could not stop playwright driver during reset: {stop_error}")
        wi.playwright = None
    wi.browser = None
    wi.context = None
    wi.page = None
    wi._connected = False


async def _probe_alive(wi) -> bool:
    """Cheap liveness check for an existing connection (2026-09-14 fix).

    ``_connected`` alone is NOT proof the browser still exists - it only records what
    happened on the previous call. Probe with real CDP round-trips:
      1. page.title()           -> proves the page target is alive; re-validate wi.page
      2. browser.is_connected() -> proves the CDP endpoint answers when no page survived

    Any exception means the connection is stale and the caller must reset + reconnect.
    Measured cost on a live connection: ~2ms.
    """
    try:
        if wi.page is not None:
            await wi.page.title()
            return True
    except Exception:
        pass  # page target dead - maybe a different tab in the same browser survives

    try:
        if wi.browser is not None and wi.context is not None:
            for p in list(wi.context.pages):
                try:
                    await p.title()
                    wi.page = p
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    try:
        if wi.browser is not None and await wi.browser.is_connected():
            return True  # endpoint alive but no usable page - caller will create one
    except Exception:
        pass

    return False


async def _acquire_page(wi) -> bool:
    """Get or create a usable page (never closes an existing one). Returns True if live.

    2026-11-06 fixes:
      * A dead tab (e.g. the page object left over from a stopped Playwright driver, whose
        target reports 'Target page, context or browser has been closed') used to re-raise and
        abort the whole connect. Now every tab is probed and, if none is alive, a fresh page is
        created - the same self-heal ensure_page() performs.
      * The destroyed-context retry slept a fixed 1s per attempt; the race resolves in
        milliseconds, so the settle wait is now PAGE_SETTLE.
    """
    try:
        for p in list(wi.context.pages):
            try:
                await p.title()
                wi.page = p
                return True
            except Exception:
                continue  # this tab's target is dead - try the next one
    except Exception as scan_error:
        logger.debug(f"connect: context.pages scan failed: {scan_error}")

    for _attempt in range(3):
        try:
            wi.page = await wi.context.new_page()
            await wi.page.title()
            return True
        except Exception as e:
            logger.warning(f"connect: could not create page (attempt {_attempt + 1}/3): {e}")
            await asyncio.sleep(PAGE_SETTLE)
    return False


async def _attach(wi, pw) -> bool:
    """CDP handshake + context/page setup on an already-started driver.

    Returns True only when a usable page exists - a browser that answers the port but yields
    no page is not a connection, so the caller keeps polling (fresh-launch path).
    """
    wi.browser = await pw.chromium.connect_over_cdp(wi.cdp_url)
    wi.context = wi.browser.contexts[0] if wi.browser.contexts else await wi.browser.new_context()
    wi._connected = await _acquire_page(wi)
    return wi._connected


def _launch_debug_chrome(port: int) -> None:
    """Launch a FRESH, SEPARATE debug-mode Chrome window (BETA 'super alfa' launch).

    Dedicated --user-data-dir means a second instance on its own profile dir opens a
    brand-new window and binds the port cleanly even when another Chrome is already
    running - it can never steal or close an existing window. There is no kill logic
    anywhere in this file (2026-09-11 contract).

    Sync on purpose: subprocess.Popen returns immediately, so there is nothing to await -
    readiness is handled by the CDP poll in connect().
    """
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    args = [chrome_path,
            f"--remote-debugging-port={port}",
            r"--user-data-dir=C:\chrome-debug-profile",
            "--no-first-run",
            "--no-default-browser-check"]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def _resolve_cdp_port() -> int:
    """CDP port from config (CHROME_CDP_PORT) - no hardcoded literal, no fallback.

    On the CLIENT the SERVER ships it via config_constants (injected exec global); on the
    SERVER 'from config import ...' works normally. If neither provides it, fail loudly.
    """
    port = globals().get("CHROME_CDP_PORT")  # path 1 - injected exec global on the CLIENT
    if port is not None:
        return port
    try:
        from config import CHROME_CDP_PORT as _imported  # path 2 - SERVER; neutralized in delivered copies
        return _imported
    except (ImportError, NameError):
        pass
    raise RuntimeError(
        "Cannot determine the Chrome CDP port for auto-launch: CHROME_CDP_PORT is not "
        "available (SERVER did not ship it via config_constants and 'config' is not "
        "importable). Set CHROME_CDP_PORT in config/config.py - no fallback port is used."
    )


async def connect(_attempts: int = 0) -> str:
    """Attach to a Chrome browser over CDP (remote debugging port).

    CONTRACT (2026-09-11, final - after the 'closed my UI' incident):
      * CDP endpoint already answers -> attach and return. The running browser is NEVER
        touched: no process killing, no window closing, no relaunching, no profile switch.
      * Nothing answers -> launch a FRESH debug-mode Chrome on its own profile dir.

    RELIABILITY (2026-09-14): a previous successful connect no longer short-circuits on the
    blind ``_connected`` flag - every call probes liveness first, resets stale state, then
    does a real re-attach or fresh launch.

    LATENCY (2026-11-06):
      * One Playwright driver per process, reused across reconnects (~1.2s to start once,
        ~0.03s per re-attach, instead of ~1.7s stop+start each time).
      * Auto-launch polls the CDP endpoint (0.25s interval, 15s cap) instead of a fixed
        5s/10s sleep - returns as soon as Chrome binds the port.
    """
    from .shared_instance import get_web_interact, set_web_interact
    from . import WebInteract as WI

    try:
        wi = get_web_interact()
        if wi is None:
            wi = WI()
            set_web_interact(wi)

        if wi._connected:
            if await _probe_alive(wi):
                if wi.page is None and wi.context is not None:
                    await _acquire_page(wi)
                if wi.page is None:
                    logger.warning("connect: alive but no usable page -- full reconnect")
                    await _reset_wi_state(wi)
                else:
                    return "Already connected to Chrome browser."
            else:
                logger.info("connect: _connected flag was stale (browser gone) -- resetting and reconnecting")
                await _reset_wi_state(wi)

        if wi.browser is not None or wi.page is not None or wi.context is not None:
            await _reset_wi_state(wi)

        from playwright.async_api import async_playwright
        if wi.playwright is None:
            wi.playwright = await async_playwright().start()

        try:
            attached = await _attach(wi, wi.playwright)
        except Exception as attach_error:
            if _is_endpoint_unreachable(str(attach_error)):
                # Nothing on the port: launch our own debug window, then poll until it is ready.
                if _attempts >= 2:
                    raise
                await _reset_wi_state(wi)  # drop the handles of the failed handshake, keep the driver
                port = await _resolve_cdp_port()
                logger.info(f"connect: no CDP endpoint ({attach_error}) -- launching debug Chrome, polling port {port}")
                _launch_debug_chrome(port)
                deadline = asyncio.get_running_loop().time() + CDP_POLL_TIMEOUT
                last_error = attach_error
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(CDP_POLL_INTERVAL)
                    await _reset_wi_state(wi)  # never stack browser handles across polls
                    try:
                        if await _attach(wi, wi.playwright):
                            break
                    except Exception as poll_error:
                        last_error = poll_error
                        await _reset_wi_state(wi)
                else:
                    await _reset_wi_state(wi)
                    raise RuntimeError(
                        f"Chrome was launched but no usable CDP page answered on port {port} within "
                        f"{CDP_POLL_TIMEOUT}s: {last_error}"
                    )
            else:
                # NOT a port problem: a stopped driver or a dead browser target (the
                # 'Target page, context or browser has been closed' case). Replacing the driver
                # fixes it; launching Chrome here would open a SECOND window for a port that is
                # already bound - exactly the 2026-11-06 incident.
                logger.info(f"connect: driver/browser state is dead ({attach_error}) -- restarting the driver")
                await _reset_wi_state(wi, stop_driver=True)
                wi.playwright = await async_playwright().start()
                if not await _attach(wi, wi.playwright):
                    raise RuntimeError(
                        f"Chrome answered the CDP port ({wi.cdp_url}) but no usable page could be "
                        "created after restarting the Playwright driver."
                    )
                attached = True
        else:
            if not attached:
                # The endpoint is live but produced no page - say so instead of claiming success.
                raise RuntimeError(
                    f"Chrome answered the CDP port ({wi.cdp_url}) but no usable page could be "
                    "created: the browser has no open tab and new_page() failed."
                )

        page_title = await wi.page.title() if wi.page else "New Tab"
        page_url = wi.page.url if wi.page else "about:blank"
        return f"""Connected to Chrome browser!

Current Page: {page_title}
URL: {page_url}
Total Pages: {len(wi.context.pages) if wi.context else 0}"""

    except Exception as e:
        error_msg = str(e)
        wi_ref = locals().get('wi')
        if wi_ref is not None:
            # Drop OUR handles only - never closes a Chrome window (2026-09-11 contract).
            await _reset_wi_state(wi_ref)
        return f"Failed to connect: {error_msg}"


# ── Anchor registration (2026-09-14 reliability fix, MODULE LEVEL on purpose) ───────
# The CLIENT source transform NEUTRALIZES same-package imports ('from .shared_instance
# import X' becomes a bare pass), so sibling tools cannot import this function directly.
# Instead we register THIS compiled copy of connect() in the process-wide anchor module;
# ensure_connected()/ensure_page() trigger reconnects through shared_instance
# .do_connect_fallback(), which resolves to exactly this function no matter which exec'd
# copy they closed over. 'set_connect_function' is a bare name on purpose: it resolves
# from the injected module globals. On the SERVER the try/except makes it a no-op.
try:
    set_connect_function(connect)  # noqa: F821 -- injected by the CLIENT loader at exec time
except Exception:  # pragma: no cover - best-effort; never break connect itself
    pass
