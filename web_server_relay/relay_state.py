"""Shared state + operator command queue for the web relay manager.

Lives in its own module so both the asyncio engine (zzz_manage_server_relay.py)
and the tkinter UI (relay_ui.py) can import it WITHOUT a circular dependency:
neither of them is imported here. The only mutable globals are STATE and CMD_Q;
all socket sends still happen inside the single engine event loop.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("relay-manager")

PING_INTERVAL_SEC = 2.0      # RTT probe cadence (a status frame rides along each tick)
PING_SAMPLES_KEEP = 60       # rolling window for min/avg/max + sparkline (last 30 drawn)
RECONNECT_MIN_DELAY = 1.0
RECONNECT_MAX_DELAY = 15.0
UI_REFRESH_MS = 250          # tkinter poll cadence


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


def request_engine_shutdown() -> None:
    """Thread-safe (call from the tkinter thread) ask-the-engine-to-stop."""
    ev, loop = STATE.stop_event, STATE.loop
    if ev is not None and loop is not None:
        loop.call_soon_threadsafe(ev.set)
