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


async def _reset_wi_state(wi) -> None:
    """Drop ALL stale connection state from a WebInteract instance.

    2026-09-14 fix (goto 'No page available' root cause): the old code set
    ``wi._connected = True`` and never cleared it, so after the browser died the flag
    stayed True forever and every later connect() short-circuited with "Already
    connected" while holding a dead page object. Any failure path must reset state so
    the NEXT call performs a real CDP handshake instead of trusting a blind flag.

    Only OUR driver handle is stopped (the node.exe Playwright process) -- stopping it
    never closes any Chrome window: CDP attach does not own the browser, and there is no
    kill logic anywhere in this file (2026-09-11 contract).
    """
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

    The ``_connected`` flag alone is NOT proof the browser still exists -- it is only a
    record of what happened on the previous call. Probe with real CDP round-trips:
      1. page.title()          -> proves the page target is alive; re-validate wi.page
                                  against context.pages (the old tab may be gone even if
                                  another one in the same browser is fine).
      2. browser.is_connected()-> proves the CDP endpoint still answers when no usable
                                  page survived (e.g. every tab was closed by the user).

    Any exception means the connection is stale and the caller must reset + reconnect.
    """
    try:
        if wi.page is not None:
            await wi.page.title()
            return True
    except Exception:
        pass  # page target dead -- maybe a different tab in the same browser survives

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
            # CDP endpoint alive but no usable page -- caller will create one.
            return True
    except Exception:
        pass

    return False


async def connect(_attempts: int = 0) -> str:
    """Attach to a Chrome browser over CDP (remote debugging port).

    CONTRACT (2026-09-11, final -- after the 'closed my UI' incident):
      * If a CDP endpoint already answers on the debug port -> attach and return.
        The running browser is NEVER touched: no process killing, no window
        closing, no relaunching, no profile switching.
      * If nothing answers -> launch a FRESH, SEPARATE Chrome window in debug
        mode with its own dedicated user-data-dir (BETA's proven 'super alfa'
        launch). A second instance on its own profile dir opens a brand-new
        window and binds the port cleanly even when another Chrome is already
        running -- it can never steal or close an existing window.

    RELIABILITY FIX (2026-09-14, after the 'goto: No page available' incident):
      * A previous successful connect no longer short-circuits blindly on the
        ``_connected`` flag -- every call first PROBES liveness (_probe_alive).
        If the browser died in between (orphan debug Chrome closed, machine
        sleep, user closed the tab), state is reset and a real re-attach or
        fresh launch happens instead of returning "Already connected" with a
        dead page object. That blind flag was exactly why goto() saw
        ``wi.page is None`` right after connect reported success.
      * Every failure path resets ALL stale state (_reset_wi_state) so no dead
        references can leak into the next call.

    SANDBOX NOTE: this source only uses subprocess.Popen with a fixed argument
    list -- exactly what the CLIENT AST guard allowlists for 'connect'. No
    socket import, no subprocess.run, no os module. Playwright performs the CDP
    handshake itself; connection refusal IS our port probe.
    """
    from .shared_instance import get_web_interact, set_web_interact
    from . import WebInteract as WI

    phase = "connect"  # which step failed: 'connect' (CDP handshake) or 'page' (after CDP ok)

    try:
        wi = get_web_interact()
        if wi is None:
            wi = WI()
            set_web_interact(wi)

        # LIVENESS PROBE before trusting the flag (2026-09-14 fix). A stale flag with a
        # dead browser used to return "Already connected" and leave every later tool
        # (goto, browser_screenshot, ...) with wi.page = None -> 'No page available'.
        if wi._connected:
            if await _probe_alive(wi):
                # Still alive. Make sure a usable page exists for the next tool call.
                try:
                    if wi.page is None and wi.context is not None:
                        if wi.context.pages:
                            wi.page = wi.context.pages[0]
                        else:
                            wi.page = await wi.context.new_page()
                        await wi.page.title()
                    return "Already connected to Chrome browser."
                except Exception as page_error:
                    logger.warning(
                        f"connect: alive check passed but no usable page ({page_error}) -- full reconnect"
                    )
            else:
                logger.info("connect: _connected flag was stale (browser gone) -- resetting and reconnecting")

        # Stale state (from a dead connection or a previously failed attempt) must be
        # cleared before starting a fresh handshake.
        if wi._connected or wi.browser is not None or wi.page is not None:
            await _reset_wi_state(wi)

        from playwright.async_api import async_playwright

        wi.playwright = await async_playwright().start()
        wi.browser = await wi.playwright.chromium.connect_over_cdp(wi.cdp_url)
        phase = "page"  # CDP handshake succeeded; the driver must NOT be stopped below

        if wi.browser.contexts:
            wi.context = wi.browser.contexts[0]
        else:
            wi.context = await wi.browser.new_context()

        # Get or create a page (never close an existing one)
        page_created = False
        for attempt in range(3):
            try:
                if wi.context.pages:
                    wi.page = wi.context.pages[0]
                else:
                    wi.page = await wi.context.new_page()
                await wi.page.title()
                page_created = True
                break
            except Exception as e:
                if "execution context was destroyed" in str(e).lower():
                    await asyncio.sleep(1)
                    continue
                raise e

        if not page_created:
            wi.page = await wi.context.new_page()

        wi._connected = True
        page_title = await wi.page.title() if wi.page else "New Tab"
        page_url = wi.page.url if wi.page else "about:blank"

        return f"""Connected to Chrome browser!

Current Page: {page_title}
URL: {page_url}
Total Pages: {len(wi.context.pages) if wi.context else 0}"""

    except Exception as e:
        error_msg = str(e)

        # ALWAYS reset stale state on failure (2026-09-14 fix): the old code stopped the
        # driver but kept _connected/page/context/browser references, which poisoned the
        # next call. Resetting is safe -- it only drops OUR handles, never touches Chrome.
        wi_ref = locals().get('wi')
        if wi_ref is not None:
            await _reset_wi_state(wi_ref)

        # ── Fresh debug-mode Chrome window (BETA launch logic, proven) ─────────────
        # Launched with a DEDICATED --user-data-dir so it ALWAYS opens as a brand-new
        # separate window and binds the port cleanly. It can never be absorbed by an
        # existing instance (different profile lock) and it can never close any
        # existing window: there is no kill logic anywhere in this file.
        if ("connect" in error_msg.lower() or "refused" in error_msg.lower()) \
                and _attempts < 2:
            try:
                # CDP port comes from config (CHROME_CDP_PORT) - no hardcoded literal. On the
                # CLIENT the SERVER ships it via config_constants (injected exec global); on the
                # SERVER "from config import ..." works normally. If neither provides it, fail
                # loudly instead of guessing a port (2026-10-02 no-fallback contract).
                _cdp_port = globals().get("CHROME_CDP_PORT")  # injected exec global on the CLIENT (path 1)
                if _cdp_port is None:
                    try:
                        from config import CHROME_CDP_PORT as _imported_cdp_port  # path 2 - SERVER; neutralized to 'pass' in delivered copies
                        _cdp_port = _imported_cdp_port
                    except (ImportError, NameError):
                        pass  # UnboundLocalError is a NameError subclass: covers the neutralized-import case
                if _cdp_port is None:
                    raise RuntimeError(
                        "Cannot determine the Chrome CDP port for auto-launch: CHROME_CDP_PORT is not "
                        "available (SERVER did not ship it via config_constants and 'config' is not "
                        "importable). Set CHROME_CDP_PORT in config/config.py - no fallback port is used."
                    )
                chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
                args = [chrome_path,
                        f"--remote-debugging-port={_cdp_port}",
                        r"--user-data-dir=C:\chrome-debug-profile",
                        "--no-first-run",
                        "--no-default-browser-check"]

                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # Longer settle wait on the second attempt (loaded machine: first
                # launch may need more than 5s to bind the port).
                await asyncio.sleep(10 if _attempts == 1 else 5)

                # Retry connection.
                return await connect(_attempts + 1)
            except Exception as start_error:
                return f"Failed to start Chrome automatically and could not connect: {start_error}"

        return f"Failed to connect: {error_msg}"


# ── Anchor registration (2026-09-14 reliability fix, MODULE LEVEL on purpose) ───────
# The CLIENT source transform NEUTRALIZES same-package imports ('from .shared_instance
# import X' becomes a bare pass), so sibling tools cannot import this function directly.
# Instead we register THIS compiled copy of connect() in the process-wide anchor module;
# ensure_connected()/ensure_page() then trigger reconnects through
# shared_instance.do_connect_fallback(), which resolves to exactly this function no
# matter which exec'd copy they closed over. 'set_connect_function' is a bare name on
# purpose: it resolves from the injected module globals (shared_instance's public symbols
# are merged into every web_interact tool's exec context). On the SERVER, where connect.py
# runs as a normal imported module and that global does not exist, the try/except makes
# registration a silent no-op -- direct calls work there anyway.
try:
    set_connect_function(connect)  # noqa: F821 -- injected by the CLIENT loader at exec time
except Exception:  # pragma: no cover - best-effort; never break connect itself
    pass
