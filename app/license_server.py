"""Coolems KG license module (SERVER side — single owner of ALL license logic).

LICENSING PLAN v14 (2026-10-15): the CLIENT keeps NO license files and never talks to
coolems.com. This module is the ONLY place that:

  - persists the registration choice + company data in <ROOT>/config/license_state.json
    (server root, gitignored);
  - checks the license with the Coolems KG endpoint on server start and once per day;
  - feeds the CLIENTs: current_status() returns the SLIM payload that rides on the
    existing SERVER->CLIENT frames (auth_ok / brain_info on connect, token_stats on
    every request round-trip):

        {"use": "private" | "company", "company": "<name>" or None,
         "email": "<e-mail>" or None, "private_licensed": true|false}   (private installs only)

The CLIENT composes the badge text from those two fields only — no license number,
no dates, nothing else ever leaves this machine.

OFFLINE MODE: any failure of the network check falls back to the cached last_check in
the state file (atomic write). An EXPIRED cache (valid_until passed) is treated as NOT
licensed even offline — clients then see "not registered" again until renewed.

    PRIVATE-USE E-MAIL CONFIRMATION (v15, refined v16 2026-10-12): a private install that HAS an
        e-mail also asks the web side (POST {"mode":"private","email":...}) on server start + once per day;
        the endpoint sends a one-time confirmation link and only writes the address into its own
        private_license.json after the user clicks it. Private individual use is FREE for commercial
        use, so the badge now reflects the confirmation state: e-mail confirmed on the web side ->
        "PRIVATE LICENSE · licensed <e-mail> for commercial use"; not (yet) confirmed / no e-mail ->
        "PRIVATE USE · license not acquired". The slim payload carries {email, private_licensed}
        for that; nothing else leaves this machine.
    
NOTE on IP tracking: ip_history lives on the WEB side now
(coolems_site/jack/licenses/ip_history.json, written by index.php). The local
state file no longer stores it.

SECURITY FRAMING (accepted by design): not DRM — the server file + UI badge are a
visible watermark and audit trail; legal enforcement stays with repository LICENSE §3.
The endpoint answer is authoritative for licensed status.
"""

import json
import os
import ssl
import urllib.request
from datetime import datetime, timezone

# ===== Paths & constants =========================================================

SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # app/ -> <ROOT>
# NOTE: state lives in config/ (a top-level license/ dir is impossible on Windows — it collides case-insensitively with the root LICENSE file)
LICENSE_DIR = os.path.join(SERVER_ROOT, "config")
STATE_FILE = os.path.join(LICENSE_DIR, "license_state.json")

# Server-side license endpoint (coolems.com). Overridable for tests / staging.
ENDPOINT_URL = os.environ.get(
    "COOLEMS_LICENSE_ENDPOINT",
    "https://coolems.com/jack/licenses/index.php",
)

CONTACT_EMAIL = "coolems.kg@gmail.com"

CHECK_TIMEOUT_SEC = 10          # one check must never hang the server
PERIODIC_CHECK_INTERVAL_SEC = 24 * 60 * 60   # re-check while running: ONCE PER DAY (background task)


# ===== State persistence ========================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def default_state() -> dict:
    return {
        "choice": None,            # "private" | "company" | None (not asked yet)
        "company": "",
        "email": "",
        "users_requested": 0,      # informational only — never sent to the endpoint
        "request_text": "",        # free text from registration; DATA ONLY: HTML-escape on render
        "created_at": _now_iso(),
        "last_check": None,        # full server answer + checked_at (offline cache)
    }


def load_state() -> dict:
    """Read the local license state. Missing/corrupt file -> fresh default (never fatal)."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_state()
        base = default_state()
        for key in base:
            if key in data:
                base[key] = data[key]
        # Defensive type checks (the file is operator-editable by design).
        if not isinstance(base.get("last_check"), dict):
            base["last_check"] = None
        return base
    except (OSError, json.JSONDecodeError):
        return default_state()


def save_state(state: dict) -> None:
    """Atomic write of the local license state. Never raises to callers."""
    try:
        os.makedirs(LICENSE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
        try:
            os.replace(tmp, STATE_FILE)
        except OSError:
            # Windows fallback when the target exists and replace fails.
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
            os.rename(tmp, STATE_FILE)
    except OSError:
        pass  # license bookkeeping must never crash the server


def record_company_registration(company: str, email: str = "", users_requested: int = 0,
                                request_text: str = "") -> dict:
    """Operator-side registration of a COMPANY install (server root state file).

    Keeps last_check on re-registration; refreshes the request data. Never raises.
    """
    state = load_state()
    state.update({
        "choice": "company",
        "company": (company or "").strip(),
        "email": (email or "").strip().lower(),
        "users_requested": max(0, int(users_requested or 0)),
        "request_text": (request_text or "").strip()[:4000],  # bound the free text
    })
    if not state.get("created_at"):
        state["created_at"] = _now_iso()
    save_state(state)
    return state


# ===== Expiry check =============================================================

def _parse_valid_until(value: str):
    """Parse a valid_until date (YYYY-MM-DD or ISO 8601) as UTC. None/empty -> perpetual."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None  # perpetual
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        # Malformed date -> fail CLOSED (treat as expired), matching the PHP side.
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def is_expired(cache: dict | None) -> bool:
    """True when a cached/licensed answer has passed its valid_until date (UTC).

    null/empty valid_until = perpetual (never expires). A malformed date fails closed.
    A cache that was never licensed cannot be 'expired' — callers check licensed first.
    """
    if not isinstance(cache, dict) or not cache.get("licensed"):
        return False
    until = _parse_valid_until(cache.get("valid_until"))
    if until is None:
        return False  # perpetual
    return datetime.now(timezone.utc) > until


# ===== Server check =============================================================

def _build_ssl_context():
    """TLS context for the endpoint. Uses the SERVER certs if present (self-signed
    deployments), otherwise the system default CA bundle."""
    try:
        from app.providers.coolems.ssl_context import detect_server_certs  # type: ignore
        certfile = detect_server_certs()
        if certfile and os.path.isfile(certfile):
            return ssl.create_default_context(cafile=certfile)
    except Exception:
        pass
    return ssl.create_default_context()


def perform_check(state: dict | None = None, timeout: int = CHECK_TIMEOUT_SEC) -> dict:
    """Check the license with the Coolems KG endpoint.

    - private installs: no network call at all.
    - company installs: HTTPS POST {company, email} to ENDPOINT_URL (GET fallback when
      no e-mail was entered). On success caches the full answer in last_check + saves.
    - OFFLINE MODE: on ANY failure falls back to the cached last_check; if never checked,
      status stays 'pending'. NEVER raises, NEVER blocks the server.
    - EXPIRY: a licensed cache whose valid_until has passed is treated as NOT licensed
      (live answer or offline fallback alike) — clients see "not registered" again.

    Returns a dict with "status": private | licensed | pending (server-side view).
    """
    state = state if state is not None else load_state()

    if state["choice"] == "private" and not state.get("email"):
        # Private install without e-mail — fully offline, nothing to confirm.
        return {"status": "private"}

    if state["choice"] == "private":
        # Private WITH e-mail: ask the web side (mode=private). The endpoint either
        # confirms it (licensed:true via:"private") or answers confirmation_pending;
        # same offline-fallback rules as company checks. NEVER raises.
        payload = {"mode": "private", "email": state["email"]}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            ENDPOINT_URL, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
            )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_build_ssl_context()) as resp:
                answer = json.loads(resp.read().decode("utf-8"))
        except Exception:
            # OFFLINE MODE — fall back to the cached last_check (a licensed private
            # cache is perpetual; a pending cache simply stays pending).
            cache = state.get("last_check") or {}
            if cache.get("licensed"):
                return {"status": "licensed", "offline": True}
            return {"status": "pending", "offline": True, "reason": "confirmation_pending"}

        state["last_check"] = {**answer, "checked_at": _now_iso()}
        save_state(state)
        if answer.get("licensed"):
            return {"status": "licensed"}
        return {"status": "pending", "reason": answer.get("reason") or "confirmation_pending"}

    if state["choice"] != "company" or not state.get("company"):
        # Not registered yet — no check possible.
        return {"status": "pending", "reason": "not_registered"}

    payload = {"company": state["company"], "email": state.get("email", "")}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT_URL,
        data=body if payload["email"] else None,   # no e-mail -> GET (IP-only match)
        headers={"Content-Type": "application/json"},
        method="POST" if payload["email"] else "GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_build_ssl_context()) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except Exception:
        # OFFLINE MODE — fall back to the cached last successful answer. An expired
        # cache does NOT count as licensed (renewal required before clients see it).
        cache = state.get("last_check") or {}
        if cache.get("licensed") and not is_expired(cache):
            return {"status": "licensed", "offline": True}
        reason = "expired" if cache.get("licensed") else None
        out = {"status": "pending", "offline": True}
        if reason:
            out["reason"] = reason
        return out

    # Successful round trip — cache the full answer (the web side tracks IP history).
    state["last_check"] = {**answer, "checked_at": _now_iso()}
    save_state(state)

    licensed = bool(answer.get("licensed")) and not is_expired(state["last_check"])
    if not licensed:
        return {"status": "pending"}
    return {"status": "licensed"}


# ===== What the CLIENTs receive (SLIM payload — nothing else leaves this box) ====

def current_status() -> dict:
    """Read-only snapshot for the SERVER->CLIENT frames. NO network call.

    Fields that travel over the wire (plan v16):
        use              "private" | "company" — what kind of install this server is;
        company          <name> or None        — only meaningful when use == "company";
        email            <e-mail> or None      — private installs: the registered e-mail;
        private_licensed true|false            — private installs: True when the web side has
                                                 CONFIRMED that e-mail (private_license.json) and the
                                                 cached answer is not expired.

    The CLIENT composes its badge from these:
        private, confirmed   -> PRIVATE LICENSE · licensed <e-mail> for commercial use
        private, unconfirmed -> PRIVATE USE · license not acquired
        company + name       -> COMMERCIAL USE — LICENSED · assigned to <name>
        company, no name     -> COMMERCIAL USE · not registered

    An EXPIRED / unconfirmed answer sends private_licensed False (and no company name) —
    clients show the unconfirmed state until the next successful check renews it.
    """
    state = load_state()
    if state["choice"] == "private":
        cache = state.get("last_check") or {}
        # Confirmed on the web side (endpoint answered licensed:true via:"private") and not
        # expired -> private_licensed True. A private license is perpetual, so expiry only
        # matters for malformed/edited answers; fail closed like company checks do.
        return {
            "use": "private",
            "company": None,
            "email": (state.get("email") or "").strip() or None,
            "private_licensed": bool(cache.get("licensed")) and not is_expired(cache),
        }

    cache = state.get("last_check") or {}
    # Licensed (live answer or offline cache) AND not expired -> send the company name.
    # Not yet confirmed OR expired -> no name: the CLIENT shows "not registered".
    if state["choice"] == "company" and cache.get("licensed") and not is_expired(cache):
        return {"use": "company",
                "company": cache.get("company") or state.get("company") or None}

    return {"use": "company", "company": None}


# ===== Periodic background loop (runs on the server event loop) ==================

async def license_loop() -> None:
    """Immediate first check, then re-check every PERIODIC_CHECK_INTERVAL_SEC.

    Runs the network call in a worker thread (await to_thread) so it NEVER blocks
    the event loop - boot and all WS traffic stay responsive even when the endpoint
    is down (the 10 s timeout would otherwise stall the whole server).
    Never raises; exits cleanly on CancelledError."""
    import asyncio as _la
    import logging
    logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")

    while True:
        try:
            st = load_state()
            needs_check = (st.get("choice") == "company" and st.get("company")) or \
                              (st.get("choice") == "private" and st.get("email"))
            if needs_check:
                result = await _la.to_thread(perform_check)
                logger.info("[SERVER] License check: status=%s", (result or {}).get("status"))
        except Exception as e:  # pragma: no cover - bookkeeping must never die
            logger.debug(f"Periodic license check failed (non-fatal): {e}")
        try:
            await _la.sleep(PERIODIC_CHECK_INTERVAL_SEC)
        except _la.CancelledError:
            raise


def start_license_task() -> "object | None":
    """Boot hook: starts the check loop on the running event loop. The FIRST check is
    done by license_loop() itself, in a worker thread - so this call NEVER blocks boot
    (a synchronous perform_check() here would stall the whole server up to 10 s when
    the endpoint is unreachable). Returns the task handle (None when no loop)."""
    import asyncio as _la
    import logging
    logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")

    try:
        loop = _la.get_running_loop()
    except RuntimeError:
        return None
    task = _la.create_task(license_loop(), name="license-check-loop")
    logger.info("[SERVER] License check loop started (immediate + every %ss)",
                PERIODIC_CHECK_INTERVAL_SEC)
    return task
