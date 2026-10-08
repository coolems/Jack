"""
COOLEMS Web Relay — local management panel (Tk GUI)
====================================================

Run this on your HOME SERVER (the machine that also runs the brain). It is a
standalone DESKTOP application (tkinter): no web server, no local port, no HTML.
It opens a management WebSocket to the PHP relay's /ws/manage endpoint and gives
you a small window to:

  * verify whether YOUR home server ("brain") is connected to the web relay,
  * measure live ping (RTT) to the relay — last/min/avg/max + sparkline,
  * see how many internet clients are currently attached (+ who they are),
  * PAUSE / RESUME new-client intake on the relay (existing clients keep flowing),
  * RESET all client connections on the relay.

Security: the /ws/manage channel is IP-locked to your home server's public egress
IP and requires the same BRAIN_KEY as the brain itself — a stranger on the
internet cannot drive the relay even if they reach it. The GUI binds to nothing;
it only runs locally in its own process.

Usage:
    python zzz_manage_server_relay.py                 # reads config/config.py (USE_WEB_SERVER)
                                                      # no relay configured yet = IDLE mode: window opens with a setup guide
    python zzz_manage_server_relay.py --relay host:port   # override the relay address

The admin API key is read from config/.api_keys.json (the entry with role=admin).
TLS trust follows the same fail-closed rules as the brain side:
WEB_RELAY_VERIFY_SSL_CERTS=True -> full CA verification; otherwise a usable
WEB_RELAY_CERT_PIN_FINGERPRINT is required. Plaintext ws:// is refused on any
non-localhost relay address (the relay must serve TLS in production anyway).

Architecture: one daemon thread runs an asyncio event loop that owns the
/websocket to the relay (reconnect + backoff, ping for RTT, status polling).
The tkinter main thread polls shared state every 250 ms and renders it. Operator
commands travel through a queue.Queue drained by an async pump task — no lock
juggling needed because all socket sends happen inside one event loop.

Modules (this folder):
    relay_state.py   shared STATE / CMD_Q + engine-shutdown helper (no tkinter, no asyncio loop)
    relay_ui.py      the tkinter dashboard window (RelayManagerUI)
    zzz_manage_server_relay.py  this file: config resolution, asyncio engine, main()

When launched as a script (python web_server_relay\\zzz_manage_server_relay.py or
via ZZZ_MANAGE_RELAY.bat), Python puts THIS folder on sys.path automatically —
the sibling modules are imported by plain name. The repo-root bootstrap below is
kept for `python -m`-style invocations from other directories.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import ssl
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

# Sibling modules (plain names: when run as a script, THIS folder is on sys.path).
from relay_state import (CMD_Q, PING_INTERVAL_SEC, RECONNECT_MAX_DELAY,
                              RECONNECT_MIN_DELAY, STATE)
from relay_ui import RelayManagerUI

# --- repo bootstrap (this file lives in web_server_relay/, repo root is one up) ---
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

log = logging.getLogger("relay-manager")


# ---------------------------------------------------------------------------
# Config resolution (runs on the home machine — same sources as the brain side)
# ---------------------------------------------------------------------------

def load_admin_api_key() -> str:
    """Read the admin API key from config/.api_keys.json (role == 'admin')."""
    keys_file = REPO_ROOT / "config" / ".api_keys.json"
    if not keys_file.exists():
        raise SystemExit(
            f"[relay-manager] {keys_file} not found — cannot find the admin API key. "
            "This panel must run on your home server."
        )
    try:
        entries = json.loads(keys_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"[relay-manager] cannot parse {keys_file}: {e}")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("role") == "admin" and entry.get("is_active", True):
            key = str(entry.get("key") or "").strip()
            if key:
                return key
    raise SystemExit(
        "[relay-manager] no active role=admin entry in config/.api_keys.json — "
        "the relay's /ws/manage channel requires the brain (admin) key."
    )


def load_relay_config(cli_override: str | None):
    """Resolve host/port/TLS from config/config.py (+ optional CLI override).

    Returns a config dict, or None when no relay address is configured at all
    (direct mode / fresh checkout) - the window then opens in IDLE mode with a
    setup guide instead of exiting. Genuinely broken setups (bad port, untrusted
    TLS, unusable cert pin) still exit loudly with an actionable error.
    """
    try:
        from config.config import (  # noqa: WPS433 - repo root package
            PROTOCOL_VERSION, USE_WEB_SERVER, WEB_RELAY_DEFAULT_PORT,
            WEB_RELAY_CERT_PIN_FINGERPRINT, WEB_RELAY_VERIFY_SSL_CERTS,
        )
    except Exception as e:
        raise SystemExit(f"[relay-manager] cannot import config.config from {REPO_ROOT}: {e}")

    address = cli_override or USE_WEB_SERVER
    if not address:
        log.info(
            "no relay configured (USE_WEB_SERVER is empty - direct mode). "
            "Opening the window in IDLE mode with setup instructions."
        )
        return None

    if ":" in address:
        host, port_s = address.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            raise SystemExit(f"[relay-manager] bad port in relay address {address!r}")
    else:
        host, port = address, WEB_RELAY_DEFAULT_PORT

    # --- TLS trust (fail-closed, same rules as ws_brain_relay.py) ---------------
    use_ssl = (port == 443) or bool(WEB_RELAY_VERIFY_SSL_CERTS) or bool(
        WEB_RELAY_CERT_PIN_FINGERPRINT and ":" not in host
    )
    is_localhost = host in ("127.0.0.1", "localhost", "::1")

    ssl_context = None
    tls_mode = ""
    if use_ssl:
        if WEB_RELAY_VERIFY_SSL_CERTS:
            ssl_context = ssl.create_default_context()
            tls_mode = "CA verify"
        elif WEB_RELAY_CERT_PIN_FINGERPRINT:
            try:
                from app.providers.coolems.ssl_pinning import build_pinned_ssl_context
                ssl_context = build_pinned_ssl_context(WEB_RELAY_CERT_PIN_FINGERPRINT)
            except Exception as e:  # pin unusable -> fail closed, do not degrade
                raise SystemExit(f"[relay-manager] cert pin unusable ({e}) — fix WEB_RELAY_CERT_PIN_FINGERPRINT")
            if ssl_context is None:
                raise SystemExit("[relay-manager] configured cert pin unusable — refusing to connect (fail-closed)")
            tls_mode = "cert pinned"
        else:
            raise SystemExit(
                "[relay-manager] relay transport requires trusted TLS but neither "
                "WEB_RELAY_VERIFY_SSL_CERTS=True nor WEB_RELAY_CERT_PIN_FINGERPRINT is set."
            )
    elif not is_localhost:
        # Non-plaintext production rule: the PHP relay serves TLS; never talk plaintext.
        raise SystemExit(
            f"[relay-manager] refusing PLAINTEXT to non-local relay {host}:{port} — "
            "set WEB_RELAY_VERIFY_SSL_CERTS=True (Let's Encrypt) or a cert pin."
        )

    return {
        "host": host,
        "port": port,
        "scheme": "wss" if use_ssl else "ws",
        "ssl_context": ssl_context,
        "tls_mode": tls_mode if use_ssl else "plaintext (local test only)",
        "protocol_version": PROTOCOL_VERSION,
    }


# ---------------------------------------------------------------------------
# Asyncio engine (runs in a daemon thread): connect, auth, ping, status, cmds
# ---------------------------------------------------------------------------

async def _send_frame(ws, obj: dict) -> None:
    await ws.send(json.dumps(obj))


def _apply_status(st: dict) -> None:
    if st.get("type") == "status":
        STATE.status = st
        STATE.last_status_at = time.time()


async def _probe_loop(ws, stop: asyncio.Event) -> None:
    """Every PING_INTERVAL_SEC: send an RTT probe + request a fresh status frame."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PING_INTERVAL_SEC)
            return  # stopped
        except asyncio.TimeoutError:
            pass
        if ws is None or STATE._ws is not ws:
            continue
        try:
            STATE._pending_ping_sent_at = time.time()
            await _send_frame(ws, {"type": "ping", "ts": round(time.time(), 3)})
            await _send_frame(ws, {"type": "status"})
        except Exception as e:
            log.warning("probe send failed: %s", e)
            return


async def _read_loop(ws, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            raw = await ws.recv()
        except Exception as e:
            STATE.note(f"relay link closed/failed: {e.__class__.__name__}: {e}")
            return
        if isinstance(raw, (bytes, bytearray)):
            continue  # protocol is text-only; ignore binary noise
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        mtype = msg.get("type")
        if mtype == "pong":
            sent_at = STATE._pending_ping_sent_at
            if sent_at is not None:
                rtt_ms = (time.time() - sent_at) * 1000.0
                STATE.ping_samples.append(rtt_ms)
                STATE.last_ping_at = time.time()
                STATE._pending_ping_sent_at = None
        elif mtype == "status":
            _apply_status(msg)
        elif mtype == "ack":
            STATE.note(f"relay ack: {msg.get('cmd')} ok={msg.get('ok')}"
                       + (f" — {msg['message']}" if msg.get("message") else ""))
        elif mtype == "error":
            STATE.last_error = str(msg.get("message", "relay error"))
            STATE.note(f"relay error: {STATE.last_error}")


async def _cmd_pump(stop: asyncio.Event) -> None:
    """Drain operator commands from the UI thread and send them to the relay."""
    while not stop.is_set():
        try:
            while True:
                cmd = CMD_Q.get_nowait()
                if STATE._ws is None or not STATE.connected:
                    STATE.note(f"command '{cmd}' failed: not connected to the relay")
                    continue
                await _send_frame(STATE._ws, {"type": cmd})
        except queue.Empty:
            pass
        await asyncio.sleep(0.25)


async def manage_relay_task(cfg: dict, api_key: str, stop: asyncio.Event) -> None:
    """Infinite connect->auth->serve loop with exponential backoff."""
    import websockets

    url = f"{cfg['scheme']}://{cfg['host']}:{cfg['port']}/ws/manage"
    delay = RECONNECT_MIN_DELAY

    while not stop.is_set():
        ws = None
        STATE.connecting = True
        try:
            conn_kwargs = {
                "open_timeout": 10,
                "close_timeout": 5,
                "max_size": 4 * 1024 * 1024,   # status frames are small; bound memory
            }
            if cfg["ssl_context"] is not None:
                conn_kwargs["ssl"] = cfg["ssl_context"]

            ws = await websockets.connect(url, **conn_kwargs)
            STATE._ws = ws
            STATE.last_error = ""

            # Auth frame — the relay IP-locks + key-checks this exactly like /ws/brain.
            await _send_frame(ws, {
                "type": "auth",
                "api_key": api_key,
                "protocol_version": cfg["protocol_version"],
            })

            # Wait for auth_ok (fail closed on anything else).
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            hello = json.loads(raw) if isinstance(raw, str) else {}
            if hello.get("type") != "auth_ok":
                STATE.last_error = f"relay rejected management auth: {hello}"
                STATE.note(STATE.last_error)
                await ws.close()
                continue

            STATE.auth_ok_info = {k: v for k, v in hello.items() if k != "message"}
            STATE.connected = True
            delay = RECONNECT_MIN_DELAY  # reset backoff on success
            STATE.note(f"management channel established with relay ({cfg['host']}:{cfg['port']})")

            probe_task = asyncio.create_task(_probe_loop(ws, stop))
            read_task = asyncio.create_task(_read_loop(ws, stop))
            cmd_task = asyncio.create_task(_cmd_pump(stop))
            await asyncio.gather(probe_task, read_task, cmd_task)
        except Exception as e:
            STATE.last_error = f"{e.__class__.__name__}: {e}"
            STATE.note(f"relay connection failed: {STATE.last_error}")
        finally:
            if ws is not None and STATE._ws is ws:
                STATE._ws = None
            try:
                if ws is not None:
                    await ws.close()
            except Exception:
                pass
            was_connected = STATE.connected
            STATE.connected = False
            STATE.connecting = False
            if was_connected and not stop.is_set():
                STATE.note("management channel lost — reconnecting")

        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return  # stopped while backing off
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, RECONNECT_MAX_DELAY)


def _engine_main(cfg: dict, api_key: str):
    """Entry point for the daemon thread: own event loop until stop is requested."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    STATE.loop = loop

    async def _run():
        stop = asyncio.Event()
        STATE.stop_event = stop
        task = asyncio.create_task(manage_relay_task(cfg, api_key, stop))
        await stop.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    try:
        loop.run_until_complete(_run())
    finally:
        STATE.loop = None
        STATE.stop_event = None
        loop.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="COOLEMS web relay management panel (Tk GUI)")
    parser.add_argument("--relay", default=None, help="override relay address host:port")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    cfg = load_relay_config(args.relay)
    if cfg is None:
        # IDLE mode: no relay configured yet. The window still opens so you can see
        # the setup guide; once USE_WEB_SERVER (or --relay) exists, it manages automatically.
        log.info("IDLE mode - no relay configured yet.")
        log.info("")
        log.info("To manage your web relay:")
        log.info("  1. Deploy web_server_relay/index.php on your web host")
        log.info("     (set ALLOWED_BRAIN_IPS + BRAIN_KEY at the top, then: php index.php --serve)")
        log.info("  2. In config/config.py set:  USE_WEB_SERVER = 'yourhost:port'")
        log.info("     and WEB_RELAY_VERIFY_SSL_CERTS = True   (Let's Encrypt cert on the relay)")
        log.info("  3. Run this app again - it will connect to the relay automatically.")
    else:
        api_key = load_admin_api_key()
        log.info("relay target: %s://%s:%s (%s), protocol v%s",
                 cfg["scheme"], cfg["host"], cfg["port"], cfg["tls_mode"], cfg["protocol_version"])
        t = threading.Thread(target=_engine_main, args=(cfg, api_key), name="relay-engine", daemon=True)
        t.start()

    log.info("Tk GUI window opening (desktop app - no web server, no port).")
    RelayManagerUI(cfg).root.mainloop()


if __name__ == "__main__":
    main()
