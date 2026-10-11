"""Licence info cache (CLIENT side) — IN-MEMORY ONLY, no files ever.

LICENSING PLAN v12 (2026-10-15): the CLIENT keeps NO licence state on disk and never
talks to coolems.com. The SERVER is the single owner of all licence logic and feeds us
a slim payload over the existing WebSocket path:

    {"use": "private" | "company", "company": "<name>" or None}

delivered in two frames (direct AND relay modes):
  - auth_ok / brain_info-merged auth_ok   -> apply_auth_info() calls update_from_frame();
  - token_stats on every request round-trip -> the provider recv loops call it.

badge_status() composes the exact shape license-badge.js renders:
    use=private            -> PRIVATE USE · not licensed for commercial use
    use=company + name     -> COMMERCIAL USE — LICENSED · assigned to <name>
    use=company, no name   -> COMMERCIAL USE · not registered
    nothing received yet   -> LICENCE NOT SET · pending server connection
"""

import logging

logger = logging.getLogger("COOLEMS")

CONTACT_EMAIL = "coolems.kg@gmail.com"

# Module-level in-memory cache. Written by update_from_frame(), read by badge_status().
_cache: dict | None = None


def update_from_frame(frame) -> None:
    """Store a licence payload received from the SERVER (auth_ok or token_stats frame).

    Accepts either the full frame dict (with "licence" key) or the bare payload.
    Invalid/missing payloads are ignored — we keep whatever was last good."""
    global _cache
    if not isinstance(frame, dict):
        return
    lic = frame.get("licence") if "licence" in frame else frame
    if not isinstance(lic, dict) or lic.get("use") not in ("private", "company"):
        return
    company = lic.get("company")
    _cache = {
        "use": lic["use"],
        "company": (str(company).strip() if company else None),
    }


def badge_status() -> dict:
    """Read-only snapshot for GET /api/licence/status — no network, no disk.

    Composes the three badge states from the slim {use, company} payload; when we have
    not heard from the SERVER yet (no WS connection made), reports LICENCE NOT SET so
    the UI can show "pending server connection"."""
    if _cache is None:
        return {
            "badge": "pending",
            "choice": None,
            "label": "LICENCE NOT SET · pending server connection",
            "line1": "LICENCE NOT SET",
            "line2": "pending server connection",
            "tooltip": ("Licence status is delivered by the COOLEMS SERVER. Connect to it and"
                        f" the badge will update automatically. Company use requires a licence"
                        f" from Coolems KG: {CONTACT_EMAIL}."),
            "company": None,
            "offline": False,
        }

    if _cache["use"] == "private":
        return {
            "badge": "private",
            "choice": "private",
            "label": "PRIVATE USE · not licensed for commercial use",
            "line1": "PRIVATE USE",
            "line2": "not licensed for commercial use",
            "tooltip": ("This installation is registered for private (individual) use only. "
                        "Commercial or company use requires a written licence from Coolems KG "
                        "(see LICENSE §3)."),
            "company": None,
            "offline": False,
        }

    if _cache["company"]:
        return {
            "badge": "licensed",
            "choice": "company",
            "label": f"COMMERCIAL USE — LICENSED · assigned to {_cache['company']}",
            "line1": "COMMERCIAL USE — LICENSED",
            "line2": f"assigned to {_cache['company']}",
            "tooltip": (f"Commercial licence for {_cache['company']} confirmed by the COOLEMS SERVER."),
            "company": _cache["company"],
            "offline": False,
        }

    return {
        "badge": "pending",
        "choice": "company",
        "label": "COMMERCIAL USE · not registered",
        "line1": "COMMERCIAL USE",
        "line2": "not registered",
        "tooltip": ("Commercial use requires a licence from Coolems KG. Register the company on"
                    f" the SERVER (licence_state.json) and confirm via {CONTACT_EMAIL} — the badge"
                    " updates automatically once the server confirms."),
        "company": None,
        "offline": False,
    }
