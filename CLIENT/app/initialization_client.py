"""
Shared provider initialization module for COOLEMS CLIENT.

Client-side only - no server providers, no auth keys.
Both code_client.py and manager.py use this function.

CRITICAL: CLIENT ONLY supports 'coolems_client' provider (WebSocket connection to SERVER).
Direct LLM access (llama.cpp) is BLOCKED — all requests MUST go through the SERVER relay.

BOOT ORDER (2026-08-21, dedup fix):
  The 2026-08-20 dedup change removed the CLIENT's local disk copies of
  app/providers/base.py and token_stats.py — they now live ONLY in SERVER and are
  delivered with every tools_response. CoolemsClientProvider subclasses BaseProvider
  at class-definition time, so the framework modules MUST be installed into
  sys.modules BEFORE get_provider() runs:

    1. bootstrap_framework()          raw WS fetch of tools_response (no provider needed)
    2. RemoteToolOrchestrator(provider=None).install_from_response(response)
                                       installs app.providers.base + token_stats in memory,
                                       plus all shared modules / DNA / config constants / prompt
    3. get_provider("coolems_client") imports client_provider -> class definition binds the
                                       REAL delivered BaseProvider; provider instance created
    4. orchestrator.provider = provider (ToolFetcher now functional for later tool_code_requests)
    5. agent, health check, model selection

      (2026-09 threadless refactor): init_provider_and_app() and _fetch_and_install_from_server()
      are now async def - the whole boot sequence runs inside ONE top-level asyncio.run(boot())
      in code_client.py before uvicorn starts. No ThreadPoolExecutor / nested asyncio.run left.

  While the SERVER is still booting / loading its model, bootstrap_framework() WAITS for it
  (time budget: BOOTSTRAP_MAX_WAIT_SEC in config/config.py) instead of crashing. A bootstrap
  that fails AFTER the wait budget is exhausted is FATAL at startup with an actionable error -
  there is no local fallback copy by design (SERVER is the single source of truth).
"""

import logging

from config import PROVIDER_DEFAULT_TIMEOUT, DB_PATH
import config  # Import module directly to allow reassigning config.MODEL_NAME
from app.providers import get_provider
from app.provider_manager import ProviderManager

logger = logging.getLogger("COOLEMS.Initialization")


async def _fetch_and_install_from_server() -> tuple:
    """Step 1+2 of boot: fetch tools_response from SERVER and install everything.

    Returns (tools_response, tool_orchestrator). bootstrap_framework() waits for a still-booting
    SERVER (time budget: BOOTSTRAP_MAX_WAIT_SEC in config/config.py); it raises RuntimeError with
    an actionable message only after the wait budget is exhausted - the CLIENT cannot start without
    tools + DNA + framework modules delivered by the SERVER.
    """
    from app.providers.bootstrap import bootstrap_framework
    from tools.remote_tools import RemoteToolOrchestrator

    # 1) Raw WS fetch (config values only — no provider class exists yet).
    response = await bootstrap_framework()
    if not isinstance(response, dict):
        raise RuntimeError(
            "CLIENT cannot start - SERVER did not deliver the framework/tools payload. "
            "Start the COOLEMS SERVER first (code.py in the server root folder) and make sure "
            "its address/port match CLIENT config settings.json, then restart the CLIENT."
        )

    # 2) Single install path — identical to every other init (lazy re-init, reconnects).
    # provider=None on purpose: the orchestrator's ToolFetcher is only needed for
# LATER tool_code_requests; step 4 wires the real provider in. NOTE: no working_root
# injection needed here - tools resolve it per turn from the conversation's DB row (2026-10-09).
    orchestrator = RemoteToolOrchestrator(provider=None)
    count = orchestrator.install_from_response(response)
    if count <= 0:
        target = _target_display()
        raise RuntimeError(
            f"SERVER at {target} did not respond with tool definitions (received 0). "
            f"The CLIENT cannot start without tools + Agent DNA delivered by the SERVER. "
            f"Check that the COOLEMS SERVER is running and reachable, then restart the CLIENT."
        )

    # Fail-closed dedup guard: get_provider() below imports client_provider which executes
    # `class CoolemsClientProvider(BaseProvider)`. If framework_sources were missing from a
    # stale/legacy SERVER response, BaseProvider would still be the pre-delivery placeholder
    # and subclassing it must NOT silently produce a broken provider — fail loudly here.
    import sys as _sys
    base_mod = _sys.modules.get("app.providers.base")
    if base_mod is None or not hasattr(base_mod, "BaseProvider"):
        raise RuntimeError(
            "SERVER response did not include framework_sources (app.providers.base). "
            "The running SERVER build predates the 2026-08-20 dedup change — restart the "
            "COOLEMS SERVER with a recent codebase, then restart the CLIENT."
        )

    logger.info(f"Remote tools initialized: {count} tools available from SERVER")
    return response, orchestrator


def _target_display() -> str:
    """Human-readable SERVER target for error messages."""
    from config import get_connection_mode, get_web_relay_address, COOLEMS_CLIENT_SERVER_ADDRESS
    if get_connection_mode() == "web_relay":
        return f"relay {get_web_relay_address() or '<not saved>'}"
    return f"direct {COOLEMS_CLIENT_SERVER_ADDRESS}"


async def init_provider_and_app(provider_name: str, api_url: str):
    """
    Initialize the AI provider for CLIENT operation.

    CLIENT-SIDE ONLY - supports 'coolems_client' provider exclusively.
    All requests flow through SERVER relay (WebSocket). Direct LLM access is blocked.

    Args:
        provider_name: MUST be "coolems_client"
        api_url: Ignored for coolems_client (uses WebSocket connection)

    Returns:
        Tuple of (provider, tool_orchestrator, agent, model_name, db_path)

    Raises:
        ValueError if any non-coolems_client provider is requested.
        RuntimeError when the SERVER cannot deliver tools/DNA/framework modules.
    """
    # ENFORCE: CLIENT can ONLY use coolems_client provider
    if provider_name != "coolems_client":
        raise ValueError(
            f"CLIENT cannot use provider '{provider_name}'. "
            f"CLIENT must connect to SERVER via 'coolems_client' (WebSocket relay). "
            f"All LLM requests MUST go through the SERVER. "
            f"To start a local UI, run code.py in the SERVER root folder."
        )

    # ── STEP 1+2: fetch tools_response and install framework + shared modules FIRST ──
    # (dedup boot order — see module docstring). This MUST happen before get_provider()
    # because CoolemsClientProvider subclasses the SERVER-delivered BaseProvider.
    try:
        _response, tool_orchestrator = await _fetch_and_install_from_server()
    except ImportError as e:
        raise RuntimeError(
            f"RemoteToolOrchestrator not available ({e}). "
            f"CLIENT cannot operate without SERVER-provided tools."
        ) from e
    except Exception as e:
        # Re-raise our own descriptive RuntimeErrors unchanged; wrap anything else.
        if isinstance(e, RuntimeError) and (
            "SERVER did not deliver" in str(e) or "framework_sources" in str(e)
        ):
            raise
        logger.error(f"[ERROR] Failed to initialize remote tools at startup ({type(e).__name__}: {e})")
        raise RuntimeError(
            f"CLIENT cannot start - SERVER did not deliver tools/DNA "
            f"({type(e).__name__}: {e}). "
            f"Start the COOLEMS SERVER first (code.py in the server root folder) and make sure "
            f"its address/port match CLIENT config settings.json, then restart the CLIENT."
        ) from e

    # ── STEP 3: NOW BaseProvider is delivered — build the real provider ──
    provider = get_provider(provider_name, api_url, PROVIDER_DEFAULT_TIMEOUT)
    ProviderManager.set_provider(provider)

    # (2026-08-26 singleton fix, defensive double-set): the module-level import at the top
    # of this file bound whichever app.provider_manager class existed BEFORE delivery. If a
    # future loader change ever replaced that module in sys.modules with the SERVER-delivered
    # copy, set_provider() above would write state into an orphaned class and delivered tool
    # code (which imports through sys.modules) would see an EMPTY singleton -> "No active
    # provider configured". Registering the provider on whatever class is CURRENTLY in
    # sys.modules makes both copies agree no matter which one wins. No-op when the loader's
    # keep-existing-local rule already holds (the normal case).
    import sys as _sys_pm
    try:
        _pm_mod = _sys_pm.modules.get("app.provider_manager")
        if _pm_mod is not None and getattr(_pm_mod, "ProviderManager", None) is not ProviderManager:
            _pm_mod.ProviderManager.set_provider(provider)
            logger.info("[INIT] Provider also registered on the sys.modules app.provider_manager class (singleton parity)")
    except Exception as e:
        logger.warning(f"[INIT] Could not mirror provider into delivered ProviderManager ({type(e).__name__}: {e})")

    # ── STEP 4: wire the provider into the orchestrator (ToolFetcher for later use) ──
    tool_orchestrator.provider = provider
    from tools.remote_tools.tool_fetcher import ToolFetcher
    tool_orchestrator._fetcher = ToolFetcher(provider)

    # Log provider info
    logger.info(f"Provider initialized: {provider.name} (WebSocket relay to SERVER)")

    # No local server startup needed - we connect to remote SERVER via WebSocket
    logger.info("[CLIENT] No local LLM server needed — all requests routed through SERVER")

    # Set OCR models (delegated to remote server)
    ProviderManager.set_ocr_models([config.MODEL_NAME])
    try:
        _pm_mod2 = _sys_pm.modules.get("app.provider_manager")
        if _pm_mod2 is not None and getattr(_pm_mod2, "ProviderManager", None) is not ProviderManager:
            _pm_mod2.ProviderManager.set_ocr_models([config.MODEL_NAME])
    except Exception as e:
        logger.warning(f"[INIT] Could not mirror OCR models into delivered ProviderManager ({type(e).__name__}: {e})")
    logger.info("Coolems client mode: OCR delegated to remote SERVER")

    # ── STEP 5: agent — DNA modules were installed from the SAME response in step 2 ──
    import types as _types
    from app.dna import get_agent
    if isinstance(get_agent, _types.ModuleType):
        # Defensive: 'from app.dna import get_agent' can bind the delivered SUBMODULE
        # (app.dna.get_agent) when the package shell was shadowed in sys.modules.
        # Unwrap to the actual function -- still SERVER-delivered code, no fallback data.
        logger.warning("[INIT] app.dna.get_agent resolved to a module - unwrapping delivered submodule")
        get_agent = getattr(get_agent, "get_agent", None)
    if not callable(get_agent):
        raise RuntimeError(
            "app.dna.get_agent is unavailable after SERVER delivery - cannot initialize agent. "
            "Check the [INIT] logs above for shared module install errors."
        )
    try:
        agent = get_agent()
    except Exception as e:
        # FIX (2026-08-19): name the real cause if DNA modules were never delivered.
        raise RuntimeError(
            f"Agent initialization failed ({type(e).__name__}: {e}). "
            f"The SERVER must deliver app.dna.* modules with its tools_response before the CLIENT can start. "
            f"Verify the running SERVER is a recent build (delivers shared_sources incl. app.dna.*) and that "
            f"the bootstrap step in this log succeeded."
        ) from e

    # Log provider capabilities
    logger.info(f"Provider capabilities: name={provider.name}, api_url={provider.api_url}")

    # Test SERVER connection health and auto-select model
    is_healthy, error_msg, models = await provider.health_check(force=True)
    if is_healthy:
        logger.info(f"SERVER connection OK. Available models: {models}")
        if models:
            config.MODEL_NAME = models[0]
            logger.info(f"Using SERVER model: {config.MODEL_NAME}")
    else:
        logger.error(f"SERVER connection FAILED: {error_msg}")

    return provider, tool_orchestrator, agent, config.MODEL_NAME, DB_PATH


def resolve_provider_and_url(cli_provider: str = None):
    """
    Resolve the final provider name and API URL for CLIENT operation.

    CLIENT-SIDE - ONLY supports coolems_client. All other providers are BLOCKED.

    Args:
        cli_provider: Optional provider override from command line (MUST be "coolems_client")

    Returns:
        Tuple of (provider_name, api_url) where provider_name is always "coolems_client"

    Raises:
        ValueError if any non-coolems_client provider is requested.
    """
    import os

    if cli_provider:
        provider_name = cli_provider.strip().lower()
    else:
        # Default to coolems_client - no other option for CLIENT
        provider_name = "coolems_client"

    if provider_name != "coolems_client":
        raise ValueError(
            f"CLIENT cannot use provider '{provider_name}'. "
            f"It must connect to SERVER via 'coolems_client' (WebSocket relay). "
            f"To start a local UI with direct LLM access, run code.py in the SERVER root folder."
        )

    api_url = ""  # coolems_client uses WebSocket, no API URL needed
    logger.info("Using coolems_client provider (WebSocket connection to SERVER)")

    return provider_name, api_url
