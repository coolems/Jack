"""Licence info cache (CLIENT side) — IN-MEMORY ONLY, no files ever.

LICENSING PLAN v16 (2026-10-15): the CLIENT keeps NO licence state on disk and never
talks to coolems.com. The SERVER is the single owner of all licence logic and feeds us
a slim payload over the existing WebSocket path:

    {"use": "private" | "company", "company": "<name>" or None,
     "email": "<e-mail>" or None, "private_licensed": true|false}   (last two: private only)

delivered in two frames (direct AND relay modes):
  - auth_ok / brain_info-merged auth_ok   -> apply_auth_info() calls update_from_frame();
  - token_stats on every request round-trip -> the provider recv loops call it.

badge_status() composes the exact shape license-badge.js renders:
    use=private + private_licensed      -> PRIVATE LICENCE · licensed <e-mail> for commercial use
    use=private (unconfirmed / no e-mail) -> PRIVATE USE · licence not acquired
    use=company + name                  -> COMMERCIAL USE — LICENSED · assigned to <name>
    use=company, no name                -> COMMERCIAL USE · not registered
    nothing received yet                -> LICENCE NOT SET · pending server connection

Private individual use is FREE for commercial use: the SERVER only sets private_licensed
when the e-mail has been CONFIRMED on the web side (private_licence.json). Old servers
that send no email/private_licensed fields simply render the unconfirmed state.
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
    email = lic.get("email")
    _cache = {
        "use": lic["use"],
        "company": (str(company).strip() if company else None),
        # Private-install fields (absent on old SERVER builds -> unconfirmed state).
        "email": (str(email).strip().lower() if email else None),
        "private_licensed": bool(lic.get("private_licensed")),
    }


def badge_status() -> dict:
    """Read-only snapshot for GET /api/licence/status — no network, no disk.

    Composes the badge states from the slim payload; when we have not heard from the
    SERVER yet (no WS connection made), reports LICENCE NOT SET so the UI can show
    "pending server connection"."""
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
        # Private individual use is FREE for commercial use — the badge only reflects
        # whether the e-mail was confirmed on the web side (SERVER: private_licensed).
        email = _cache.get("email")
        if _cache.get("private_licensed") and email:
            return {
                "badge": "private-licensed",
                "choice": "private",
                "label": f"PRIVATE LICENCE · licensed {email} for commercial use",
                "line1": "PRIVATE LICENCE",
                "line2": f"licensed {email} for commercial use",
                "tooltip": (f"Free private (individual) licence, confirmed for {email}. "
                            "Includes free commercial use as an individual."),
                "company": None,
                "offline": False,
            }
        if _cache.get("private_licensed"):
            # Confirmed but no e-mail on the payload (defensive; shouldn't happen).
            return {
                "badge": "private-licensed",
                "choice": "private",
                "label": "PRIVATE LICENCE · licensed for commercial use",
                "line1": "PRIVATE LICENCE",
                "line2": "licensed for commercial use",
                "tooltip": ("Free private (individual) licence confirmed by the COOLEMS SERVER. "
                            "Includes free commercial use as an individual."),
                "company": None,
                "offline": False,
            }
        # No e-mail given at setup OR confirmation still pending on the web side.
        return {
            "badge": "private-pending",
            "choice": "private",
            "label": "PRIVATE USE · licence not acquired",
            "line1": "PRIVATE USE",
            "line2": "licence not acquired",
            "tooltip": ("Private individual use of COOLEMS is free (including commercial use"
                        " as an individual). Add your e-mail in the SERVER's licence state to"
                        f" activate it — confirmation arrives by e-mail. Contact: {CONTACT_EMAIL}."),
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
