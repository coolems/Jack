"""
COOLEMS SERVER - Just Relay Launcher (Server Root)

Pure relay server — NO intelligence, NO tools, NO logic, NO UI.

Provider modes:
  - coolems_server   → Headless WebSocket brain relay ONLY (port from config.COOLEMS_DIRECT_WS_PORT,
                        overridable with --port / port=NNN). Accepts client connections, forwards to
                        llama_server, streams back.
  - llama            → Same just relay + launches CLIENT as subprocess for local UI use.
                        SERVER stays dumb. CLIENT handles all intelligence locally.

Information flow:
  CLIENT (remote or local) -> SERVER (WebSocket on COOLEMS_DIRECT_WS_PORT) → llama_server → back through chain

SERVER does NOTHING except relay. All tool execution, agentic logic, and conversation
management happens on the CLIENT side.

Usage:
  python code.py                              # Auto-detect from config (default: coolems_server)
  python code.py provider=coolems_server      # Headless relay only (no UI)
  python code.py provider=llama               # Relay + launches CLIENT subprocess for local UI
  python code.py --port 9000                  # Override the WebSocket relay port only (CLIENT UI stays on CLIENT_UI_PORT)
"""

import atexit
import asyncio
import logging
import os
import subprocess
import sys

from app.logging_config import setup_logging
from app.initialization import init_provider_and_app, resolve_provider_and_url


# ===== Ensure SERVER root is on sys.path (required when running from ROOT venv) =====
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

# == Logging Setup ==
logger = logging.getLogger("COOLEMS")
setup_logging()  # Every COOLEMS.* message (relay, llama provider, pipe drainers) is written to logs/coolems.log + errors.log.


def _parse_args():
    """Parse command line arguments.

    Returns:
        Tuple of (provider_arg: str | None, port_arg: int | None).
    """
    provider_arg = None
    port_arg = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("provider="):
            provider_arg = arg.split("=", 1)[1].strip().lower()
            i += 1
        elif arg == "--port" and i + 1 < len(args):
            try:
                port_arg = int(args[i + 1])
                i += 2
            except ValueError:
                logger.warning("Invalid --port value, ignoring")
                i += 2
        elif arg.startswith("port="):
            try:
                port_arg = int(arg.split("=", 1)[1])
                i += 1
            except ValueError:
                logger.warning("Invalid port= value, ignoring")
                i += 1
        else:
            i += 1

    return provider_arg, port_arg


def _apply_port_override(port):
    """Apply a CLI --port override to the WebSocket server port (single source of truth)."""
    if port is None:
        return
    import config as cfg
    original = cfg.COOLEMS_DIRECT_WS_PORT
    cfg.COOLEMS_DIRECT_WS_PORT = int(port)
    logger.info("WebSocket port overridden via CLI: %s -> %s", original, cfg.COOLEMS_DIRECT_WS_PORT)


def _log_relay_banner(mode_note, final_api_url):
    """Log the startup banner for a relay run mode (headless or + local CLIENT).

    Reads host/port from the config MODULE via attribute access (not `from ... import`
    value snapshots) so CLI --port overrides applied by _apply_port_override() are
    always reflected — same live values the WS server binds in
    server_provider._run_direct_ws_server(). Log-vs-bind agreement holds by
    construction regardless of call ordering. Review fix: replaces two duplicated
    banner blocks that each bound port snapshots at import time.
    """
    import config as cfg

    logger.info("=" * 60)
    logger.info("COOLEMS Just Relay Server%s Starting", mode_note)
    logger.info("=" * 60)

    logger.info("COOLEMS JUST RELAY SERVER started (no UI, no tools, no logic)")
    backend = getattr(cfg, "SERVER_BACKEND", "llama")
    logger.info("Local LLM Backend: %s (%s)", backend, final_api_url)

    logger.info("Listening for client connections on %s:%s...", cfg.COOLEMS_SERVER_HOST, cfg.COOLEMS_DIRECT_WS_PORT)


async def _run_relay_async(provider_obj, final_api_url):
    """Run the just relay server entirely on asyncio event loop - no threads."""
    _log_relay_banner("", final_api_url)

    try:
        await provider_obj.start_all_servers()
    finally:
        logger.info("Shutting down just relay server...")
        await provider_obj.stop_all_servers()


CLIENT_UI_PORT = 8000  # HTTP port of the local CLIENT web UI (matches code_client.py --port default; unrelated to the WS relay port)

async def _run_relay_with_client_async(provider_obj, final_api_url):
    """Run just relay + launch CLIENT as subprocess for local UI."""
    _log_relay_banner(" + Local CLIENT", final_api_url)

    # Start relay task
    relay_task = asyncio.create_task(provider_obj.start_all_servers(), name="relay-server")

    # Wait a moment for relay to be ready, then launch CLIENT subprocess
    await asyncio.sleep(2)  # Give relay time to bind port

    client_script = os.path.join(_script_dir, "CLIENT", "code_client.py")
    venv_python = os.path.join(_script_dir, "CLIENT", "venv", "Scripts", "python.exe")

    if not os.path.exists(venv_python):
        # Fallback to system python if CLIENT venv doesn't exist
        venv_python = sys.executable

    logger.info("Launching local CLIENT from: %s", client_script)
    logger.info("Using Python: %s", venv_python)

    try:
        client_proc = subprocess.Popen(
            [venv_python, client_script, "--port", str(CLIENT_UI_PORT)],
            cwd=os.path.join(_script_dir, "CLIENT"),
            stdout=None,  # Show CLIENT output directly in console
            stderr=None,
        )

        logger.info("Local CLIENT process started (PID: %s)", client_proc.pid)

        # Verify CLIENT is still alive after a few seconds
        await asyncio.sleep(3)
        if client_proc.poll() is not None:
            logger.error(f"CLIENT crashed immediately with code {client_proc.returncode}")
            logger.info("Relay server still running. Start CLIENT manually.")
        else:
            logger.info("Local CLIENT is alive - Open browser at http://127.0.0.1:%s/", CLIENT_UI_PORT)

    except Exception as e:
        logger.error(f"Failed to launch CLIENT subprocess: {e}")
        logger.info("Relay server is still running. Start CLIENT manually with:")
        logger.info("  cd CLIENT && python code_client.py")

    # Run relay task forever (until cancelled)
    try:
        await relay_task
    except asyncio.CancelledError:
        logger.info("Relay task cancelled (shutdown)")
    finally:
        # Clean up CLIENT subprocess
        if 'client_proc' in locals() and client_proc.poll() is None:
            logger.info("Stopping local CLIENT process...")
            try:
                client_proc.terminate()
                client_proc.wait(timeout=5)
            except Exception:
                client_proc.kill()

        await provider_obj.stop_all_servers()


if __name__ == "__main__":
    # 0. Config bootstrap (2026-08-23): create missing config data files from
    # their shipped example templates so a fresh checkout never fails at startup.
    from config.bootstrap import ensure_server_config_files
    _created_configs = ensure_server_config_files()
    if _created_configs:
        logger.info("Config bootstrap created missing file(s): " + ", ".join(_created_configs))
    # 1. Parse arguments FIRST (before any provider-dependent initialization)
    _parsed_provider_arg, port_arg = _parse_args()

    # 2. Determine final provider name and URL
    if _parsed_provider_arg:
        resolved_provider_name = _parsed_provider_arg.lower().strip()
    else:
        import config as cfg
        resolved_provider_name = cfg.PROVIDER.lower().strip()

    # B3 (2026-09-02): validate the provider name up front. A typo like 'llam' used to
    # fall into the relay+CLIENT launch path and only fail later in resolve_provider_and_url()
    # after heavy startup work; reject it here with a clear message instead.
    _VALID_SERVER_PROVIDERS = ("llama", "coolems_server")
    if resolved_provider_name not in _VALID_SERVER_PROVIDERS:
        logger.error(
            f"Unknown provider '{resolved_provider_name}'. "
            "Valid providers: 'llama', 'coolems_server'."
        )
        sys.exit(2)

    # Track original request to distinguish headless-only from relay+client
    want_local_client = (resolved_provider_name != "coolems_server")

    # Map llama → coolems_server so we get the just relay wrapper
    if resolved_provider_name == "llama":
        final_provider_name = "coolems_server"  # Use CoolemsServerProvider which wraps Llama
        logger.info(f"Using CoolemsServerProvider (just relay + local CLIENT for UI, backend={resolved_provider_name})")
    else:
        final_provider_name = resolved_provider_name

    final_provider_name, final_api_url = resolve_provider_and_url(cli_provider=final_provider_name)

    # 3. Apply CLI port override to the single source of truth (config.COOLEMS_DIRECT_WS_PORT)
    _apply_port_override(port_arg)

    # 4. Initialize provider ONLY (no tool orchestrator, no agent — just relay).
    # (2026-09 threadless refactor): init_provider_and_app() is async now - the boot
    # sequence (LLM launch + health wait) runs in ONE top-level asyncio.run(_boot()) on a
    # single event loop, mirroring what code_client.py does. This is the only asyncio.run
    # for boot; the relay then starts its own loop afterwards and nothing from boot
    # outlives that loop (no lingering tasks/sockets).
    async def _boot():
        return await init_provider_and_app(final_provider_name, final_api_url)

    provider, _, _, MODEL_NAME = asyncio.run(_boot())

    # ===== Cleanup on Exit =====
    atexit.register(lambda: logger.info("COOLEMS Just Relay Server says goodbye. Until next time!"))

    # Dedicated OCR llama-server cleanup (2026-08-27): never leave an orphaned GPU process on exit.
    def _ocr_exit_cleanup():
        try:
            from app.providers.llama import ocr_server_manager
            ocr_server_manager.shutdown_ocr_server()
        except Exception as e:
            logger.warning(f"OCR server cleanup on exit failed (ignored): {e}")

    atexit.register(_ocr_exit_cleanup)

    # ===== Run based on mode =====
    if want_local_client:
        # llama mode: relay + launch CLIENT subprocess for local UI
        try:
            asyncio.run(_run_relay_with_client_async(provider, final_api_url))
        except KeyboardInterrupt:
            logger.info("Just relay server stopped by user (KeyboardInterrupt)")
            sys.exit(130)
    else:
        # coolems_server mode: headless relay ONLY
        try:
            asyncio.run(_run_relay_async(provider, final_api_url))
        except KeyboardInterrupt:
            logger.info("Just relay server stopped by user (KeyboardInterrupt)")
            sys.exit(130)
