"""Tkinter UI for the web relay manager (dark dashboard window).

Imported by zzz_manage_server_relay.py AFTER its asyncio engine is wired up.
The tkinter import is guarded so that merely importing this module on a Python
without Tcl/Tk fails with an actionable message instead of a bare ImportError.
Shared state comes from relay_state.py (no circular dependency: relay_state
imports nothing from this package's other modules).
"""

from __future__ import annotations

import logging

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:  # Python installed without Tcl/Tk
    raise SystemExit(
        "[relay-manager] tkinter is not available in this Python installation.\n"
        "Reinstall Python from python.org and make sure 'tcl/tk and IDLE' is ticked."
    )

from relay_state import CMD_Q, STATE, UI_REFRESH_MS, request_engine_shutdown

log = logging.getLogger("relay-manager")


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
