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
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

# --- repo bootstrap (this file lives in web_server_relay/, repo root is one up) ---
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import messagebox
except ImportError:  # Python installed without Tcl/Tk
    raise SystemExit(
        "[relay-manager] tkinter is not available in this Python installation.\n"
        "Reinstall Python from python.org and make sure 'tcl/tk and IDLE' is ticked."
    )

log = logging.getLogger("relay-manager")

PING_INTERVAL_SEC = 2.0      # RTT probe cadence (a status frame rides along each tick)
PING_SAMPLES_KEEP = 60       # rolling window for min/avg/max + sparkline (last 30 drawn)
RECONNECT_MIN_DELAY = 1.0
RECONNECT_MAX_DELAY = 15.0
UI_REFRESH_MS = 250          # tkinter poll cadence

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
# Shared state (updated by the asyncio engine thread, read by the tkinter UI)
# ---------------------------------------------------------------------------

class RelayState:
    def __init__(self):
        self.connected = False          # WS open AND authenticated
        self.connecting = False         # handshake in progress
        self.last_error = ""            # human-readable reason when not connected
        self.auth_ok_info: dict = {}    # echoed protocol_version / manage_id from the relay

        self.ping_samples: deque[float] = deque(maxlen=PING_SAMPLES_KEEP)  # ms, oldest->newest
        self.last_ping_at: float | None = None
        self._pending_ping_sent_at: float | None = None
        self._ws = None                 # current websockets connection (engine thread only)

        self.status: dict = {}          # latest 'status' frame from the relay
        self.last_status_at: float | None = None

        self.events: list[tuple[int, str]] = []   # (seq, text), newest last
        self._ev_seq = 0

        self.loop: asyncio.AbstractEventLoop | None = None   # engine loop (for thread-safe shutdown)
        self.stop_event: asyncio.Event | None = None

    def note(self, msg: str):
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._ev_seq += 1
        self.events.append((self._ev_seq, f"[{stamp}] {msg}"))
        if len(self.events) > 200:
            del self.events[:len(self.events) - 200]
        log.info(msg)

    @property
    def ping_stats(self) -> dict:
        if not self.ping_samples:
            return {"last": None, "min": None, "avg": None, "max": None}
        s = list(self.ping_samples)
        return {
            "last": round(s[-1], 1),
            "min": round(min(s), 1),
            "avg": round(sum(s) / len(s), 1),
            "max": round(max(s), 1),
        }

    def snapshot(self, cfg: dict | None) -> dict:
        st = self.status or {}
        relay_view = {
            "address": f"{cfg['host']}:{cfg['port']}" if cfg else "",
            "scheme": (cfg or {}).get("scheme", ""),
            "tls_mode": (cfg or {}).get("tls_mode", ""),
            "protocol_version": (cfg or {}).get("protocol_version"),
        }
        return {
            "setup_needed": cfg is None,
            "panel_connected": self.connected,
            "connecting": self.connecting,
            "last_error": self.last_error,
            "relay": relay_view,
            "ping_ms": self.ping_stats,
            "sparkline": [round(x, 1) for x in list(self.ping_samples)[-30:]],
            "status_age_sec": (time.time() - self.last_status_at) if self.last_status_at else None,
            "relay_uptime_sec": st.get("relay_uptime_sec"),
            "brain_connected": bool(st.get("brain_connected")),
            "paused": bool(st.get("paused")),
            "clients_total": int(st.get("clients_total", 0)),
            "clients": st.get("clients", []),
            "recent_events": [e.get("msg") for e in (st.get("recent_events") or [])][-50:],
        }


STATE = RelayState()
CMD_Q: queue.Queue[str] = queue.Queue()   # operator commands UI -> engine thread


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


def request_engine_shutdown() -> None:
    """Thread-safe (call from the tkinter thread) ask-the-engine-to-stop."""
    ev, loop = STATE.stop_event, STATE.loop
    if ev is not None and loop is not None:
        loop.call_soon_threadsafe(ev.set)


# ---------------------------------------------------------------------------
# Tkinter UI
# ---------------------------------------------------------------------------

class Palette:
    BG = "#0b0e14"
    PANEL = "#121722"
    LINE = "#263141"
    TEXT = "#d7e0ef"
    DIM = "#8b98ad"
    OK = "#5ad1a3"
    WARN = "#f5c542"
    ERR = "#ff6b6b"
    ACC = "#8ab4ff"


def _fmt_uptime(s) -> str:
    if s is None:
        return "—"
    s = int(s)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    out = ""
    if d:
        out += f"{d}d "
    if h or d:
        out += f"{h}h "
    return out + f"{m}m {s % 60}s"


class RelayManagerUI:
    def __init__(self, cfg: dict | None):
        self.cfg = cfg

        self.root = tk.Tk()
        self.root.title("COOLEMS Web Relay Manager")
        self.root.configure(bg=Palette.BG)
        self.root.geometry("900x680")
        self.root.minsize(760, 560)

        f_base = ("Consolas", 10)
        f_big = ("Consolas", 20, "bold")
        f_head = ("Consolas", 9)

        # ---------- header ----------------------------------------------------
        head = tk.Frame(self.root, bg=Palette.BG)
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(head, text="COOLEMS Web Relay — management panel", font=f_big,
                 fg=Palette.ACC, bg=Palette.BG).pack(side="left")
        self.relay_line = tk.Label(self.root, text="loading…", font=f_head,
                                   fg=Palette.DIM, bg=Palette.BG)
        self.relay_line.pack(anchor="w", padx=16)

        # ---------- setup banner (idle mode only) ------------------------------
        self.banner = tk.Frame(self.root, bg="#1c1709", highlightbackground="#5a4a1e",
                               highlightthickness=1)
        b_inner = tk.Frame(self.banner, bg="#1c1709")
        b_inner.pack(fill="x", padx=12, pady=10)
        tk.Label(b_inner, text="Setup needed — no relay configured yet (idle mode)",
                 font=("Consolas", 11, "bold"), fg=Palette.WARN, bg="#1c1709").pack(anchor="w")
        guide = (
            "This window is idle because USE_WEB_SERVER in config/config.py is empty (direct LAN mode).\n\n"
            "1. Deploy web_server_relay/index.php on your web host and run it:  php index.php --serve\n"
            "     (set ALLOWED_BRAIN_IPS + BRAIN_KEY at the top of that file first)\n"
            "2. In config/config.py set:   USE_WEB_SERVER = 'yourhost:port'   ·   WEB_RELAY_VERIFY_SSL_CERTS = True\n"
            "3. Run this app again — it connects to the relay automatically.\n\n"
            "Or pass the address directly:  python zzz_manage_server_relay.py --relay host:port"
        )
        tk.Label(b_inner, text=guide, font=f_head, fg=Palette.TEXT, bg="#1c1709",
                 justify="left").pack(anchor="w", pady=(6, 0))

        # ---------- card grid ---------------------------------------------------
        grid = tk.Frame(self.root, bg=Palette.BG)
        grid.pack(fill="both", expand=True, padx=14, pady=8)
        for i in range(2):
            grid.columnconfigure(i, weight=1, uniform="cards")
            grid.rowconfigure(0 if i == 0 else 1, weight=1, uniform="rows")

        # --- card: panel -> relay link
        c1 = self._card(grid, 0, 0, "PANEL → RELAY LINK")
        self.link_label = tk.Label(c1, text="connecting…", font=f_big, bg=Palette.PANEL)
        self.link_label.pack(anchor="w", pady=(2, 8))
        self.last_error_lbl = self._kv(c1, "last error: ", "—")
        self.status_age_lbl = self._kv(c1, "status age: ", "—")

        # --- card: ping
        c2 = self._card(grid, 0, 1, "PING TO RELAY (RTT)")
        ping_row = tk.Frame(c2, bg=Palette.PANEL)
        ping_row.pack(anchor="w", pady=(2, 4))
        self.ping_last_lbl = tk.Label(ping_row, text="—", font=f_big, bg=Palette.PANEL)
        self.ping_last_lbl.pack(side="left")
        tk.Label(ping_row, text=" ms", font=("Consolas", 12), fg=Palette.DIM,
                 bg=Palette.PANEL).pack(side="left", padx=(4, 0))
        self.ping_stats_lbl = self._kv(c2, "min — · avg — · max — ms", "")
        self.spark = tk.Canvas(c2, width=300, height=56, bg="#0d1119",
                               highlightbackground=Palette.LINE, highlightthickness=1)
        self.spark.pack(anchor="w", pady=(8, 0))

        # --- card: brain
        c3 = self._card(grid, 1, 0, "YOUR HOME SERVER (BRAIN)")
        self.brain_label = tk.Label(c3, text="…", font=f_big, bg=Palette.PANEL)
        self.brain_label.pack(anchor="w", pady=(2, 8))
        self.uptime_lbl = self._kv(c3, "relay uptime: ", "—")
        self.paused_lbl = self._kv(c3, "intake: ", "—")

        # --- card: clients
        c4 = self._card(grid, 1, 1, "CLIENTS ON THE RELAY")
        count_row = tk.Frame(c4, bg=Palette.PANEL)
        count_row.pack(anchor="w", pady=(2, 6))
        self.client_count_lbl = tk.Label(count_row, text="—", font=f_big, bg=Palette.PANEL)
        self.client_count_lbl.pack(side="left")
        self.brain_hint_lbl = tk.Label(count_row, text="", font=("Consolas", 9),
                                       fg=Palette.DIM, bg=Palette.PANEL)
        self.brain_hint_lbl.pack(side="left", padx=(8, 0))

        style = ttk_style_dark(self.root)
        self.tree = _build_treeview(c4, style)
        self.tree.pack(fill="both", expand=True, pady=(2, 0))

        # ---------- operator actions -------------------------------------------
        acts = tk.Frame(self.root, bg=Palette.BG)
        acts.pack(fill="x", padx=14, pady=(6, 2))
        btn_kw = dict(font=("Consolas", 10), relief="flat", bd=0, cursor="hand2",
                      activebackground="#22304a", activeforeground="#ffffff")
        self.btn_pause = tk.Button(acts, text="⏸ Pause new clients", bg="#1a2333",
                                   fg=Palette.TEXT, **btn_kw)
        self.btn_resume = tk.Button(acts, text="▶ Resume client intake", bg="#1a2333",
                                    fg=Palette.TEXT, **btn_kw)
        self.btn_reset = tk.Button(acts, text="✕ Reset ALL client connections",
                                   bg="#3a1d1d", fg=Palette.ERR, **{**btn_kw, "activebackground": "#542828"})
        self.btn_pause.pack(side="left")
        self.btn_resume.pack(side="left", padx=(10, 0))
        self.btn_reset.pack(side="left", padx=(10, 0))
        tk.Label(acts, text="pause = existing clients keep flowing · reset = every internet client is disconnected (they reconnect on their own)",
                 font=("Consolas", 8), fg=Palette.DIM, bg=Palette.BG).pack(side="left", padx=(14, 0))

        self.btn_pause.config(command=lambda: self._send_cmd("pause"))
        self.btn_resume.config(command=lambda: self._send_cmd("resume"))
        self.btn_reset.config(command=self._confirm_reset)

        # ---------- event log ----------------------------------------------------
        log_card = tk.Frame(self.root, bg=Palette.PANEL, highlightbackground=Palette.LINE,
                            highlightthickness=1)
        log_card.pack(fill="x", padx=14, pady=(6, 12))
        tk.Label(log_card, text="EVENT LOG (relay + local)", font=f_head, fg=Palette.DIM,
                 bg=Palette.PANEL).pack(anchor="w", padx=10, pady=(8, 2))
        self.log_text = tk.Text(log_card, height=9, wrap="none", bg="#0d1119", fg=Palette.TEXT,
                                insertbackground=Palette.TEXT, relief="flat", font=("Consolas", 9),
                                state="disabled")
        sb = _make_scrollbar(log_card, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        sb.pack(side="right", fill="y", pady=(0, 10), padx=(0, 8))

        self._last_ev_seq = 0
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(UI_REFRESH_MS, self._tick)

    # -- widget helpers ---------------------------------------------------------

    def _card(self, parent: tk.Frame, row: int, col: int, title: str) -> tk.Frame:
        card = tk.Frame(parent, bg=Palette.PANEL, highlightbackground=Palette.LINE,
                        highlightthickness=1)
        card.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 8),
                  pady=(0 if row == 0 else 8))
        tk.Label(card, text=title, font=("Consolas", 9), fg=Palette.DIM,
                 bg=Palette.PANEL).pack(anchor="w", padx=12, pady=(10, 4))
        return card

    def _kv(self, parent: tk.Frame, label: str, value: str) -> tk.Label:
        row = tk.Frame(parent, bg=Palette.PANEL)
        row.pack(anchor="w", padx=12, pady=1)
        tk.Label(row, text=label, font=("Consolas", 9), fg=Palette.DIM,
                 bg=Palette.PANEL).pack(side="left")
        v = tk.Label(row, text=value, font=("Consolas", 9), fg=Palette.TEXT,
                     bg=Palette.PANEL)
        v.pack(side="left", padx=(4, 0))
        return v

    # -- actions ------------------------------------------------------------------

    def _send_cmd(self, cmd: str):
        if self.cfg is None or not STATE.connected:
            messagebox.showwarning("Not connected", "Cannot send command — the panel is not connected to the relay.")
            return
        CMD_Q.put_nowait(cmd)

    def _confirm_reset(self):
        if self.cfg is None or not STATE.connected:
            messagebox.showwarning("Not connected", "Cannot send command — the panel is not connected to the relay.")
            return
        ok = messagebox.askyesno(
            "Confirm reset",
            "Disconnect ALL internet clients from the relay?\n\n"
            "Every attached client will be closed (code 1001). They will try to reconnect on their own.",
            icon="warning")
        if ok:
            CMD_Q.put_nowait("reset_clients")

    def on_close(self):
        request_engine_shutdown()   # engine thread exits on its own (daemon)
        self.root.destroy()

    # -- rendering ------------------------------------------------------------------

    def _tick(self):
        try:
            self._render(STATE.snapshot(self.cfg))
        except Exception as e:  # never let a render bug kill the UI loop
            log.exception("UI tick failed: %s", e)
        self.root.after(UI_REFRESH_MS, self._tick)

    def _set_dot_label(self, lbl: tk.Label, color: str, text: str):
        lbl.config(text=f"● {text}", fg=color)

    def _render(self, d: dict):
        # banner + relay line
        if d["setup_needed"]:
            self.banner.pack(fill="x", padx=14, pady=(0, 8), after=self.relay_line)
            self.relay_line.config(text="idle — no relay configured (see setup guide below)")
        else:
            self.banner.pack_forget()
            r = d["relay"]
            self.relay_line.config(
                text=f"relay {r['address']} · {r['scheme'].upper()} ({r['tls_mode']}) · protocol v{r['protocol_version']}")

        # panel link card
        if d["panel_connected"]:
            self._set_dot_label(self.link_label, Palette.OK, "connected & authenticated")
        elif d["connecting"]:
            self._set_dot_label(self.link_label, Palette.WARN, "connecting…")
        else:
            self._set_dot_label(self.link_label, Palette.ERR, "disconnected (auto-retrying)")
        self.last_error_lbl.config(text=d["last_error"] or "—",
                                   fg=Palette.ERR if d["last_error"] and not d["panel_connected"] else Palette.TEXT)
        age = d["status_age_sec"]
        self.status_age_lbl.config(text="no status yet" if age is None else f"{int(age)}s ago")

        # ping card
        p = d["ping_ms"]
        self.ping_last_lbl.config(text="—" if p["last"] is None else str(p["last"]))
        fmt = lambda v: "—" if v is None else str(v)  # noqa: E731
        self.ping_stats_lbl.config(
            text=f"min {fmt(p['min'])} · avg {fmt(p['avg'])} · max {fmt(p['max'])} ms")
        self._draw_spark(d["sparkline"])

        # brain card
        if not d["panel_connected"]:
            self._set_dot_label(self.brain_label, Palette.ERR, "unknown (panel offline)")
        elif d["brain_connected"]:
            self._set_dot_label(self.brain_label, Palette.OK, "CONNECTED to relay")
        else:
            self._set_dot_label(self.brain_label, Palette.ERR, "NOT connected — check your server / USE_WEB_SERVER")
        self.uptime_lbl.config(text=_fmt_uptime(d["relay_uptime_sec"]))
        if d["paused"]:
            self.paused_lbl.config(text="PAUSED (new clients rejected)", fg=Palette.WARN)
        else:
            self.paused_lbl.config(text="open", fg=Palette.TEXT)

        # clients card
        total = d["clients_total"]
        self.client_count_lbl.config(text=str(total) if d["panel_connected"] else "—")
        self.brain_hint_lbl.config(
            text="(brain offline)" if (d["panel_connected"] and not d["brain_connected"]) else "")

        # clients table
        seen = set()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for c in d["clients"]:
            cid = str(c.get("id", ""))
            if cid in seen:
                continue
            seen.add(cid)
            self.tree.insert("", "end", values=(cid, c.get("email") or "?",
                                                c.get("role") or "?", c.get("ip") or "?"))

        # buttons
        on = d["panel_connected"] and not d["setup_needed"]
        paused = bool(d["paused"])
        self.btn_pause.config(state="normal" if (on and not paused) else "disabled")
        self.btn_resume.config(state="normal" if (on and paused) else "disabled")
        self.btn_reset.config(state="normal" if on else "disabled")

        # local event log (engine thread notes)
        new_events = [t for seq, t in STATE.events if seq > self._last_ev_seq]
        if new_events:
            self._last_ev_seq = max(seq for seq, _ in STATE.events)
            self.log_text.config(state="normal")
            for line in new_events:
                self.log_text.insert("end", line + "\n")
            # trim to ~200 lines
            if int(self.log_text.index("end-1c").split(".")[0]) > 200:
                self.log_text.delete("1.0", f"{int(self.log_text.index('end-1c').split('.')[0]) - 200}.0")
            self.log_text.see("end")
            self.log_text.config(state="disabled")

    def _draw_spark(self, samples: list[float]):
        c = self.spark
        c.delete("all")
        if len(samples) < 2:
            return
        w, h = 300, 56
        mx = max(max(samples), 50.0)
        pts: list[float] = []
        n = len(samples)
        for i, v in enumerate(samples):
            x = 1 + (i / (n - 1)) * (w - 2)
            y = (h - 2) - (v / mx) * (h - 6)
            pts.extend((x, y))
        c.create_line(*pts, fill=Palette.OK, width=1)


def ttk_style_dark(root: tk.Tk):
    from tkinter import ttk
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("Treeview", background="#0d1119", fieldbackground="#0d1119",
                    foreground=Palette.TEXT, rowheight=20, borderwidth=0)
    style.configure("Treeview.Heading", background=Palette.PANEL, foreground=Palette.DIM,
                    font=("Consolas", 9), relief="flat")
    return style


def _build_treeview(parent: tk.Frame, style):
    from tkinter import ttk
    tree = ttk.Treeview(parent, columns=("id", "email", "role", "ip"),
                        show="headings", selectmode="browse")
    for col, text, width in (("id", "ID", 90), ("email", "EMAIL", 230),
                             ("role", "ROLE", 100), ("ip", "IP", 120)):
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="w")
    return tree


def _make_scrollbar(parent: tk.Frame, command):
    from tkinter import ttk
    sb = ttk.Scrollbar(parent, orient="vertical", command=command)
    return sb


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
