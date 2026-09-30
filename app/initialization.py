"""
Server provider initialization module.

SERVER ONLY — Supports 'llama' as local backend via coolems_server.
Handles initialization for:
  - coolems_server  : Headless WebSocket brain relay (uses SERVER_BACKEND)
  - llama           : Direct llama.cpp backend (legacy mode)
"""

import os
import glob
import logging

from config import PROVIDER_DEFAULT_TIMEOUT, PROVIDER as CONFIG_PROVIDER, PROFILES_FILE
import config
from app.providers import get_provider
from app.provider_manager import ProviderManager

logger = logging.getLogger("COOLEMS.Initialization")


def _resolve_ocr_models_from_profiles() -> list:
    """Resolve OCR model filenames from profile ocr_model paths.

    Reads profiles.json and for each profile with an 'ocr_model' directory,
    finds the actual .gguf model file (excluding mmproj files). Returns a
    deduplicated list of model basenames to use for OCR transcription.
    """
    if not os.path.exists(PROFILES_FILE):
        logger.warning(f"Profiles file not found at {PROFILES_FILE}")
        return []

    try:
        import json
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            profiles = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load profiles.json: {e}")
        return []

    ocr_model_names = []
    seen_dirs = set()

    for role_name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue

        ocr_dir = profile.get("ocr_model")
        if not ocr_dir or not isinstance(ocr_dir, str):
            continue

        # Deduplicate by directory to avoid scanning same folder twice
        abs_dir = os.path.abspath(ocr_dir)
        if abs_dir in seen_dirs:
            continue
        seen_dirs.add(abs_dir)

        if not os.path.isdir(abs_dir):
            logger.warning(f"OCR model directory not found for role '{role_name}': {abs_dir}")
            continue

        # Find .gguf files excluding mmproj/projector files
        gguf_files = glob.glob(os.path.join(abs_dir, "*.gguf"))
        for fpath in gguf_files:
            fname_lower = os.path.basename(fpath).lower()
            if "mmproj" in fname_lower or "projector" in fname_lower:
                continue
            basename = os.path.basename(fpath)
            if basename not in ocr_model_names:
                ocr_model_names.append(basename)

    return ocr_model_names


async def init_provider_and_app(provider_name: str, api_url: str):
    """
    Initialize the AI provider for server operation.

    SERVER ONLY — Supports 'llama' (direct), or 'coolems_server' (relay with configurable backend).

    Args:
        provider_name: "llama" or "coolems_server"
        api_url: The API endpoint URL for the local LLM backend

    (2026-09 threadless refactor): async def - the boot sequence runs inside ONE top-level
        asyncio.run(_boot()) in code.py before the relay loop starts. start_server() and
        health_check() are awaited on that same loop; no threads, no nested loops.

        Returns:
        Tuple of (provider, tool_orchestrator, agent, model_name)
        
        Note: For coolems_server headless mode, tool_orchestrator and agent are None.
              For llama mode with UI, these are initialized normally.
    """

    from app.providers.llama.model_persistence import get_loaded_model_name as _get_last_model_name
    # 2026-08-23: persisted last-loaded model name (source of truth in config/.last_model_loaded.json)
    # Create provider
    provider = get_provider(provider_name, api_url, PROVIDER_DEFAULT_TIMEOUT)

    ProviderManager.set_provider(provider)

    logger.info(f"Provider initialized: {provider.name} (api_url={provider.api_url})")

    # AUTO-START the local LLM server
    logger.info("Starting provider server(s)...")
    start_result = await provider.start_server()
    if start_result:
        logger.info("Provider server started successfully.")
    else:
        logger.warning("Provider startup had issues - may still work if already running.")

    # Set OCR models from profiles.json ocr_model paths
    if provider_name in ("llama", "coolems_server"):
        ocr_models = _resolve_ocr_models_from_profiles()
        if ocr_models:
            ProviderManager.set_ocr_models(ocr_models)
            logger.info(f"OCR models resolved from profiles: {ocr_models}")
        else:
            # 2026-08-23: config.MODEL_NAME no longer exists (source of truth is now
            # .last_model_loaded.json). Fall back to the persisted last-loaded model,
            # or an empty list if nothing has ever been loaded.
            _fallback_ocr = _get_last_model_name()
            if _fallback_ocr:
                ProviderManager.set_ocr_models([_fallback_ocr])
                logger.warning("No OCR models found in profiles, falling back to last loaded model: %s", _fallback_ocr)
            else:
                ProviderManager.set_ocr_models([])
                logger.warning("No OCR models found in profiles and no last-loaded model recorded yet")

    # For coolems_server headless mode: no tool orchestrator or agent needed
    if provider_name == "coolems_server":
        tool_orchestrator = None  # Not used in headless server mode
        agent = None
        logger.info("ToolOrchestrator and Agent skipped (headless server mode)")
    else:
        # For llama mode with UI - these would be needed if tools/logic were present
        tool_orchestrator = None  # Tools are in CLIENT/ folder now
        agent = None

    # Test provider health
    is_healthy, error_msg, models = await provider.health_check(force=True)
    if is_healthy:
        if provider_name == "coolems_server":
            backend = config.SERVER_BACKEND
            logger.info(f"[SERVER] === Available {backend} models on startup ({len(models)}): {models} ===")
        else:
            logger.info(f"Provider health check PASSED. Available models: {models}")
            # 2026-08-23: report which persisted model (source of truth) is available.
            _last_model = _get_last_model_name()
            if models and _last_model:
                logger.info(f"Last loaded model '{_last_model}' is available")
            elif models:
                _missing = _last_model if _last_model else "(none recorded)"
                logger.warning(f"Last loaded model '{_missing}' not found in available models: {models}")
    else:
        logger.warning(f"Provider health check FAILED: {error_msg}")

    return provider, tool_orchestrator, agent, _get_last_model_name()


def resolve_provider_and_url(cli_provider: str = None):
    """
    Resolve the final provider name and API URL for server operation.

    SERVER ONLY — Supports 'llama' (direct), or 'coolems_server' (relay).
    When coolems_server is used, config.SERVER_BACKEND determines the local LLM engine.

    Priority order:
      1. CLI argument (provider=...)
      2. Environment variable PROVIDER
      3. config.py PROVIDER setting

    Args:
        cli_provider: Optional provider override from command line ('llama' or 'coolems_server')

    Returns:
        Tuple of (provider_name, api_url)
    """
    if cli_provider:
        provider_name = cli_provider.strip().lower()
    else:
        provider_name = os.environ.get("PROVIDER", CONFIG_PROVIDER).lower().strip()

    if provider_name == "llama":
        api_url = os.environ.get("LLAMA_URL", config.LLAMA_URL)
        logger.info(f"Using llama.cpp provider at {api_url}")
    elif provider_name == "coolems_server":
        backend = config.SERVER_BACKEND
        if backend == "llama":
            api_url = os.environ.get("LLAMA_URL", config.LLAMA_URL)
            logger.info(f"Using coolems_server provider with local llama.cpp at {api_url}")
        else:
            # Default to llama if SERVER_BACKEND is misconfigured
            api_url = os.environ.get("LLAMA_URL", config.LLAMA_URL)
            logger.warning(
                f"Unknown SERVER_BACKEND '{backend}', defaulting to llama.cpp at {api_url}"
            )
    else:
        raise ValueError(
            f"Unknown server provider: '{provider_name}'. "
            f"Valid server providers: 'llama', 'coolems_server'. "
            f"For client providers, use the CLIENT/ folder."
        )

    logger.info(f"Resolved provider: {provider_name}, API URL: {api_url}")
    return provider_name, api_url
