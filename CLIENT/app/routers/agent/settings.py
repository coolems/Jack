"""Settings endpoints: GET/POST /api/settings (connection mode, relay host/port,
server address(es), runtime-tab fields). Verbatim move from the original flat agent.py."""

from typing import Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import logging

logger = logging.getLogger("COOLEMS")


def register(router: APIRouter, provider, model_name: str, api_timeout: int,
             agent, stop_events) -> None:
    """Attach this endpoint group to *router* (verbatim move from the original flat file)."""
    # ===== SETTINGS ENDPOINT =====
    @router.get("/api/settings")
    async def get_settings():
        """Return current client settings from settings.json.

        Returns the stored settings (server_address) plus the live-resolved address
        values so the UI always shows the active one. Used by the Settings modal.
        """
        try:
            from config import _load_settings, COOLEMS_CLIENT_SERVER_ADDRESS, _resolve_client_server_address, WEB_RELAY_UI_ENABLED, get_runtime_settings

            settings = _load_settings()

            # (2026-09-23) Connection Mode menu: always return the three fields so the UI can
            # render them even when never saved (defaults: direct / empty host / empty port).
            settings.setdefault("connection_mode", "direct")

            # (2026-09-23) Relay UI availability flag: when False the UI hides the whole
            # Connection Mode group; report direct so no relay state is displayed.
            settings["web_relay_ui_enabled"] = WEB_RELAY_UI_ENABLED
            if not WEB_RELAY_UI_ENABLED:
                settings["connection_mode"] = "direct"
            settings.setdefault("relay_host", "")
            settings.setdefault("relay_port", "")

            # (2026-10-02) Runtime tab: python_exec dialog behavior + auto-approve delay.
            # Read live from settings.json so the UI always shows the current values.
            runtime = get_runtime_settings()
            settings["python_exec_auto_approve"] = runtime["python_exec_auto_approve"]
            settings["python_exec_auto_approve_seconds"] = runtime["python_exec_auto_approve_seconds"]
            # (2026-10-03) Runtime tab: image generator - model line + default resolution.
            settings["image_gen_tier"] = runtime["image_gen_tier"]
            settings["image_gen_width"] = runtime["image_gen_width"]
            settings["image_gen_height"] = runtime["image_gen_height"]

            # Return both values:
            # 1. current_server_address - the value active at startup (module-level cached)
            # 2. resolved_server_address - freshly re-resolved NOW (reflects latest settings.json changes)
            settings["current_server_address"] = COOLEMS_CLIENT_SERVER_ADDRESS
            settings["resolved_server_address"] = _resolve_client_server_address()

            return settings
        except Exception as e:
            logger.warning(f"Failed to load settings: {e}")
            return {"server_address": "", "current_server_address": ""}

    @router.post("/api/settings")
    async def save_settings(data: Dict):
        """Save client settings to settings.json.

        Supported fields:
          - connection_mode: "direct" (default) | "web_relay" — how the CLIENT talks to
            the SERVER. web_relay routes through the public web_server_relay (single-file PHP relay).
          - relay_host: str hostname/IP of the relay (required for web_relay mode)
          - relay_port: int 1-65535 (optional; WEB_RELAY_DEFAULT_PORT applies when empty)
          - server_address: str (IP:port format, e.g., "192.168.1.100:8080")
          - server_addresses: list[str] ordered failover addresses; index 0 is tried
            FIRST (put localhost there so local wins), the rest only when unreachable

        Runtime tab fields (2026-10-02):
          - python_exec_auto_approve: "manual" (default) | "auto" — python_exec dialog behavior
          - python_exec_auto_approve_seconds: int 1..3600 — auto-approve delay in seconds

        Runtime tab fields (2026-10-03):
          - image_gen_tier: "auto" (default) | "high" | "mid" | "low" — which model line
            generate_image() uses. 'auto' = classify by probed GPU VRAM; the others pin that
            tier (same semantics as SERVER config IMAGE_GEN_TIER_OVERRIDE, but per-machine).
          - image_gen_width / image_gen_height: int pixels 32..4096 — the DEFAULT resolution
            generate_image() uses when no explicit size is passed. The worker snaps values down
            to multiples of 32 and clamps them to the tier's max_pixels budget, so oversized
            values degrade gracefully instead of breaking generation.

        NOTE: Changes to server address(es) apply from the NEXT connection onward
        (the failover list is re-read on every connect); boot-time display values
        refresh on CLIENT restart.
        """
        try:
            from config import _load_settings, _save_settings, WEB_RELAY_UI_ENABLED

            # Load existing settings and merge with new values
            current = _load_settings()

            def _validate_addr(addr):
                """Return an error detail string for a bad address, or None when valid."""
                if ":" not in addr:
                    return "Server address must include port (e.g., 192.168.1.100:8080)"
                host_part = addr.rsplit(":", 1)[0]
                port_part = addr.rsplit(":", 1)[1]
                if not host_part:
                    return "Server address must have a non-empty host/IP (e.g., 192.168.1.100:8080)"
                if not port_part.isdigit():
                    return f"Port '{port_part}' is not a valid number. Must be between 1 and 65535."
                port_num = int(port_part)
                if not (1 <= port_num <= 65535):
                    return f"Port '{port_part}' is out of range. Must be between 1 and 65535."
                return None

            # (2026-09-23) Connection Mode menu fields.
            if "connection_mode" in data:
                mode = str(data["connection_mode"]).strip().lower()
                if mode not in ("direct", "web_relay"):
                    return JSONResponse(status_code=400, content={"detail": "connection_mode must be 'direct' or 'web_relay'"})
                if mode == "web_relay" and not WEB_RELAY_UI_ENABLED:
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "Internet Web Relay is disabled on this CLIENT (WEB_RELAY_UI_ENABLED=False in config/config.py)"}
                    )
                current["connection_mode"] = mode

            if "relay_host" in data:
                relay_host = str(data.get("relay_host") or "").strip()
                # basic hostname/IP sanity (no whitespace, no path)
                if relay_host and (any(c.isspace() for c in relay_host) or "/" in relay_host):
                    return JSONResponse(status_code=400, content={"detail": f"Invalid relay host {relay_host!r}"})
                current["relay_host"] = relay_host

            # (2026-10-02) Runtime tab fields: python_exec dialog behavior.
            #   python_exec_auto_approve        : "manual" (default, wait for the user) | "auto"
            #   python_exec_auto_approve_seconds: 1..3600 (dialog auto-runs after this many seconds)
            if "python_exec_auto_approve" in data:
                mode = str(data["python_exec_auto_approve"]).strip().lower()
                if mode not in ("manual", "auto"):
                    return JSONResponse(status_code=400, content={"detail": "python_exec_auto_approve must be 'manual' or 'auto'"})
                current["python_exec_auto_approve"] = mode

            if "python_exec_auto_approve_seconds" in data:
                raw_sec = str(data.get("python_exec_auto_approve_seconds") or "").strip()
                try:
                    sec = int(raw_sec)
                except (TypeError, ValueError):
                    return JSONResponse(status_code=400, content={"detail": "python_exec_auto_approve_seconds must be a whole number of seconds"})
                if not (1 <= sec <= 3600):
                    return JSONResponse(status_code=400, content={"detail": "python_exec_auto_approve_seconds must be between 1 and 3600"})
                current["python_exec_auto_approve_seconds"] = sec

            # (2026-10-03) Runtime tab field: image generator tier. generate_image reads this
            # LIVE from settings.json on every run, so the pick applies to the very next
            # generation - no restart needed.
            if "image_gen_tier" in data:
                tier = str(data["image_gen_tier"]).strip().lower()
                if tier not in ("auto", "high", "mid", "low"):
                    return JSONResponse(status_code=400, content={"detail": "image_gen_tier must be one of 'auto', 'high', 'mid', 'low'"})
                current["image_gen_tier"] = tier

            # (2026-10-03) Runtime tab fields: default image resolution. generate_image reads
            # these LIVE from settings.json on every run, so a save applies to the very next
            # generation - no restart needed. 32..4096 px per dimension; the worker snaps down
            # to multiples of 32 and clamps to the tier's max_pixels budget anyway.
            for _size_key in ("image_gen_width", "image_gen_height"):
                if _size_key in data:
                    raw_size = str(data.get(_size_key) or "").strip()
                    try:
                        size_val = int(raw_size)
                    except (TypeError, ValueError):
                        return JSONResponse(status_code=400, content={"detail": f"{_size_key} must be a whole number of pixels"})
                    if not (32 <= size_val <= 4096):
                        return JSONResponse(status_code=400, content={"detail": f"{_size_key} must be between 32 and 4096 pixels"})
                    current[_size_key] = size_val

            if "relay_port" in data:
                raw_port = str(data.get("relay_port") or "").strip()
                if raw_port == "":
                    current["relay_port"] = ""  # empty -> default port at connect time
                elif not raw_port.isdigit() or not (1 <= int(raw_port) <= 65535):
                    return JSONResponse(status_code=400, content={"detail": f"Relay port {raw_port!r} must be a number between 1 and 65535"})
                else:
                    current["relay_port"] = int(raw_port)

            # web_relay mode requires a relay host - fail fast with an actionable message.
            if current.get("connection_mode") == "web_relay" and not str(current.get("relay_host") or "").strip():
                return JSONResponse(status_code=400, content={"detail": "Internet Web Relay mode needs a relay host (IP or domain)"})

            if "server_address" in data:
                addr = str(data["server_address"]).strip()
                # Validate format - should be IP:port or hostname:port
                if addr:
                    err = _validate_addr(addr)
                    if err:
                        return JSONResponse(status_code=400, content={"detail": err})
                current["server_address"] = addr

            if "server_addresses" in data:
                # Ordered failover list - index 0 is tried FIRST (local first), the rest
                # only when the previous address cannot be reached. Empty list clears it.
                raw_list = data.get("server_addresses")
                items = raw_list if isinstance(raw_list, list) else [raw_list]
                cleaned = []
                for item in items:
                    addr = str(item).strip()
                    if not addr:
                        continue
                    err = _validate_addr(addr)
                    if err:
                        return JSONResponse(status_code=400, content={"detail": f"{addr}: {err}"})
                    if addr not in cleaned:
                        cleaned.append(addr)
                current["server_addresses"] = cleaned

            _save_settings(current)

            logger.info(f"Settings saved: {current}")

            return {"status": "ok", "message": "Settings saved successfully.", **current}
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            return JSONResponse(
                status_code=500,
                content={"detail": f"Failed to save settings: {str(e)}"}
            )


    pass  # fallback; every register() above attaches at least one route
