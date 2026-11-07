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

OFFLINE MODE (2026-11-09, narrowed): the cache fallback applies ONLY when the endpoint is
genuinely UNREACHABLE (DNS/socket/timeout). An expired cache (valid_until passed) is
treated as NOT licensed even offline — clients see "not registered" again until renewed.

A TLS TRUST failure or a bad endpoint answer is NOT offline: it raises LicenseCheckError
with the reason (cert verification, a refused redirect, HTTP status, malformed JSON).
Falling back to the cache there would let a MITM look like a valid license — the endpoint
answer is authoritative, so an unverifiable answer must never be answered from cache.

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
import logging
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger("COOLEMS.License")

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
    """Read the local license state. Missing/corrupt file -> fresh default, LOUDLY logged.

    Never fatal (the server must boot), but the operator sees WHY the state is empty:
    a corrupt or unreadable state file used to silently reset licensing to 'not asked'.
    """
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        # Normal first-run state — not an error.
        return default_state()
    except json.JSONDecodeError as e:
        logger.error("license_state.json is CORRUPT (%s) — treating licensing as "
                     "not registered; fix or re-run the licensing step", e)
        return default_state()
    except OSError as e:
        logger.error("license_state.json unreadable (%s: %s) — treating licensing as "
                     "not registered", type(e).__name__, e)
        return default_state()

    if not isinstance(data, dict):
        logger.error("license_state.json is not a JSON object (%s) — treating licensing "
                     "as not registered", type(data).__name__)
        return default_state()
    base = default_state()
    for key in base:
        if key in data:
            base[key] = data[key]
    # Defensive type checks (the file is operator-editable by design).
    if not isinstance(base.get("last_check"), dict):
        base["last_check"] = None
    return base


def save_state(state: dict) -> None:
    """Atomic write of the local license state. Never raises to callers, but logs loudly.

    A failed write means the offline cache is NOT updated — the next unreachable check
    will use an older (or no) answer, so it must be visible in the log.
    """
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
    except OSError as e:
        logger.error("license_state.json write FAILED (%s: %s) — the offline cache is "
                     "not updated; %s", type(e).__name__, e, STATE_FILE)


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

class LicenseCheckError(Exception):
    """The license endpoint could not be reached VERIFIABLY.

    Raised by perform_check() instead of falling back to the cache, so the operator sees
    WHY (TLS trust, a refused redirect, HTTP status, malformed answer). Cache fallback
    stays for genuine reachability failures only — see _is_unreachable().
    """


# Network-level failures = "the endpoint is not reachable right now" -> cache fallback OK.
# Everything else = the answer could not be verified -> raise, never guess.
# ssl.SSLError is deliberately NOT here: the request path wraps it in URLError (OSError),
# and a wrapped SSLError (cert verify failed) is a TRUST failure, not an outage.
_UNREACHABLE_ERRORS = (
    urllib.error.URLError,        # DNS / refused / connection reset (wraps socket errors)
    ConnectionError,              # socket-level, incl. reset
    TimeoutError,                 # urlopen timeout (Python 3.10+)
)


def _is_unreachable(exc: Exception) -> bool:
    """True when the failure means 'no endpoint answer', not 'unverified answer'.

    HTTPError is NEVER unreachable: an HTTP status arrived, so the endpoint answered and
    we must not replace that answer with the cache (it is already converted to
    LicenseCheckError in _request_endpoint — this guard keeps the classification correct
    even if the call order ever changes).

    URLError carries the underlying reason: a wrapped socket/timeout error is unreachable,
    but a wrapped SSLCertVerificationError is a TRUST failure -> NOT unreachable.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return not isinstance(exc.reason, ssl.SSLError)
    return isinstance(exc, _UNREACHABLE_ERRORS)


def _build_ssl_context() -> ssl.SSLContext:
    """TLS trust for the license endpoint: system CA bundle + hostname verification.

    coolems.com is a public CA-signed host (measured live 2026-11-07: subject
    CN=coolems.com, issuer "Let's Encrypt / YR1"). ssl.create_default_context() gives
    CERT_REQUIRED + check_hostname=True against the OS trust store — the correct trust
    model for a public host, the same one a browser uses.

    NO certificate pin, deliberately (2026-11-07). Let's Encrypt leaf certs are valid for
    ~90 days (measured window: 89 days), so a leaf SHA-256 pin would break every license
    check at each rotation and need a manual re-read ~4x per year. The reliable
    alternative — pinning the ISSUER/chain — is not enforceable here: ssl exposes only the
    leaf via getpeercert(), and ssl.SSLContext.verify_callback is documented as unreliable
    across CPython/OpenSSL builds (see app/providers/coolems/ssl_pinning.py header). A pin
    that cannot be enforced correctly is worse than no pin. ssl_pinning stays for the relay
    link, where the cert IS self-signed and pinning is the right tool.

    NO local-cert branch either. The old code imported
    app.providers.coolems.ssl_context.detect_server_certs, a symbol that never existed
    (git log -S shows only the commit that added the import line), and pointed cafile at
    the LAN self-signed cert (CN=coolems-direct-ws, no CA extension) — that can never
    verify a public chain, so the branch would have failed TLS and silently used system CAs.
    """
    return ssl.create_default_context(ssl.Purpose.SERVER_AUTH)


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect on the license request.

    urllib follows HTTPS -> HTTP redirects, re-sends the POST body in PLAINTEXT and then
    accepts the plaintext 200 as the license answer (verified live 2026-11-07). The
    endpoint answer is authoritative for licensed status, so an answer arriving over an
    unverified connection must never be used — and the registered e-mail must never leave
    the box unencrypted. Refusing keeps both guarantees: the failure surfaces as
    LicenseCheckError naming the redirect target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LicenseCheckError(
            f"license endpoint redirected ({code}) {req.full_url} -> {newurl} — refusing "
            "to follow: the license answer must come from the configured endpoint over TLS "
            "(point COOLEMS_LICENSE_ENDPOINT at the real URL)"
        )


# Trust is decided once, so the request path is fixed too: one module-level opener with
# the CA-verified HTTPS handler and the no-redirect policy. urlopen(context=...) would
# rebuild an opener per call and still follow redirects.
_OPENER = urllib.request.build_opener(
    _RefuseRedirect(),
    urllib.request.HTTPSHandler(context=_build_ssl_context()),
)


def _trust_failure_message(exc: Exception) -> str:
    """Plain-language TLS trust failure for the license endpoint."""
    host = urllib.request.urlsplit(ENDPOINT_URL).netloc
    return (f"license endpoint TLS verification failed for {host}: "
            f"{type(exc).__name__}: {exc}")


def _request_endpoint(req, timeout: int) -> dict:
    """One HTTPS round trip to the license endpoint. Raises on anything unverifiable.

    Raises:
      LicenseCheckError  - TLS trust failure (cert verification), a redirect (refused),
                           HTTP status != 200, non-JSON / non-object answer.
      URLError / OSError - unreachable (the caller may then use the cache fallback).
    """
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise LicenseCheckError(
                    f"license endpoint answered HTTP {resp.status} for {ENDPOINT_URL} "
                    f"(method={req.get_method()}) — answer not usable"
                )
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # urlopen raises HTTPError for non-2xx before the status check above can run.
        # The endpoint's error body is the ONLY way to learn WHY it failed (the server
        # answers a JSON {reason, detail} on a fatal), so read it best-effort and quote it.
        detail = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if body:
            try:
                err = json.loads(body)
                if isinstance(err, dict) and err.get("reason"):
                    detail = f" — server reason={err['reason']}"
                    if err.get("detail"):
                        detail += f": {err['detail']}"
            except json.JSONDecodeError:
                detail = f" — body={body[:120]!r}"
        raise LicenseCheckError(
            f"license endpoint answered HTTP {e.code} for {ENDPOINT_URL} "
            f"(method={req.get_method()}) — answer not usable{detail}"
        ) from e
    except ssl.SSLError as e:
        # Trust failure. The request path also surfaces it wrapped as
        # URLError(reason=SSLError) — both paths raise here, never the cache.
        raise LicenseCheckError(_trust_failure_message(e)) from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            raise LicenseCheckError(_trust_failure_message(e.reason)) from e
        raise

    try:
        answer = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LicenseCheckError(
            f"license endpoint returned non-JSON for {ENDPOINT_URL}: "
            f"{raw[:120]!r} ({e})"
        ) from e
    if not isinstance(answer, dict):
        raise LicenseCheckError(
            f"license endpoint returned {type(answer).__name__}, expected a JSON object "
            f"for {ENDPOINT_URL}: {raw[:120]!r}"
        )
    return answer


def _perform_one_check(state: dict, payload: dict, method: str, timeout: int) -> dict:
    """POST/GET the endpoint once and turn the answer into a server-side result.

    Cache fallback ONLY on unreachable (the result then carries "offline": True);
    anything unverifiable raises LicenseCheckError.
    """
    body = json.dumps(payload).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(
        ENDPOINT_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        answer = _request_endpoint(req, timeout)
    except LicenseCheckError:
        raise
    except Exception as e:
        if not _is_unreachable(e):
            raise LicenseCheckError(
                f"license check failed for {ENDPOINT_URL}: "
                f"{type(e).__name__}: {e}"
            ) from e
        cache = state.get("last_check") or {}
        if payload.get("mode") == "private":
            # A private license is perpetual; a pending cache simply stays pending.
            if cache.get("licensed"):
                return {"status": "licensed", "offline": True}
            return {"status": "pending", "offline": True, "reason": "confirmation_pending"}
        if cache.get("licensed") and not is_expired(cache):
            return {"status": "licensed", "offline": True}
        out = {"status": "pending", "offline": True}
        if cache.get("licensed"):
            out["reason"] = "expired"
        return out

    state["last_check"] = {**answer, "checked_at": _now_iso()}
    save_state(state)

    if payload.get("mode") == "private":
        if answer.get("licensed"):
            return {"status": "licensed"}
        return {"status": "pending",
                "reason": answer.get("reason") or "confirmation_pending"}

    licensed = bool(answer.get("licensed")) and not is_expired(state["last_check"])
    return {"status": "licensed"} if licensed else {"status": "pending"}


def perform_check(state: dict | None = None, timeout: int = CHECK_TIMEOUT_SEC) -> dict:
    """Check the license with the Coolems KG endpoint.

    - private installs WITHOUT e-mail: no network call at all.
    - private installs WITH e-mail: HTTPS POST {mode:"private", email}.
    - company installs: HTTPS POST {company, email} (GET when no e-mail — IP-only match).
    - On success the full answer is cached in last_check + saved.
    - EXPIRY: a licensed cache whose valid_until has passed is treated as NOT licensed
      (live answer or offline fallback alike) — clients see "not registered" again.

    Failure policy (2026-11-09 — no blind fallback):
      unreachable (DNS / refused / timeout / reset) -> cached last_check, "offline": True.
      anything else -> raises LicenseCheckError with the reason. TLS trust failures,
      refused redirects, HTTP != 200 and malformed JSON are NEVER answered from cache:
      the endpoint answer is authoritative, so an answer we could not verify must not be
      replaced by a guess.

    Raises:
      LicenseCheckError  - the endpoint could not be verified.
    Returns:
      {"status": "private" | "licensed" | "pending", ...} — "offline": True only on the
      cache fallback; "reason" carries confirmation_pending / expired / not_registered.
    """
    state = state if state is not None else load_state()

    if state["choice"] == "private" and not state.get("email"):
        # Private install without e-mail — fully offline, nothing to confirm.
        return {"status": "private"}

    if state["choice"] == "private":
        # Private WITH e-mail: ask the web side (mode=private). The endpoint either
        # confirms it (licensed:true via:"private") or answers confirmation_pending.
        payload = {"mode": "private", "email": state["email"]}
        return _perform_one_check(state, payload, "POST", timeout)

    if state["choice"] != "company" or not state.get("company"):
        # Not registered yet — no check possible.
        return {"status": "pending", "reason": "not_registered"}

    payload = {"company": state["company"], "email": state.get("email", "")}
    return _perform_one_check(state, payload,
                              "POST" if payload["email"] else "GET", timeout)


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
    Exits cleanly on CancelledError; every other failure is REPORTED, never swallowed."""
    import asyncio as _la
    logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")

    while True:
        try:
            st = load_state()
            needs_check = (st.get("choice") == "company" and st.get("company")) or \
                              (st.get("choice") == "private" and st.get("email"))
            if needs_check:
                result = await _la.to_thread(perform_check)
                status = (result or {}).get("status")
                if (result or {}).get("offline"):
                    # Cache fallback: the endpoint was unreachable, not unverified.
                    logger.warning("[SERVER] License check: status=%s from CACHE "
                                   "(endpoint unreachable)", status)
                else:
                    logger.info("[SERVER] License check: status=%s", status)
        except LicenseCheckError as e:
            # The endpoint could not be VERIFIED - this is an operator-visible problem
            # (TLS trust, pin config, HTTP status, malformed answer), not "offline".
            logger.error("[SERVER] License check FAILED (not verified, no cache used): %s", e)
        except Exception as e:  # pragma: no cover - the loop itself must never die
            logger.error("[SERVER] License check loop error (non-fatal): %s: %s",
                         type(e).__name__, e)
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
    logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")

    try:
        _la.get_running_loop()   # raises RuntimeError when no loop is running
    except RuntimeError:
        return None
    task = _la.create_task(license_loop(), name="license-check-loop")
    logger.info("[SERVER] License check loop started (immediate + every %ss)",
                PERIODIC_CHECK_INTERVAL_SEC)
    return task
