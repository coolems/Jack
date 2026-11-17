"""Coolems KG license module (SERVER side — single owner of all license logic).

- config/license_state.json (gitignored) holds the registration choice and the last
  endpoint answer, used as the offline cache.
- perform_check() runs at boot and once per day against the license endpoint.
- current_status() returns the slim payload that rides on the auth_ok / brain_info and
  token_stats frames: {"use", "company", "email", "private_licensed"}. Nothing else
  leaves this machine; the CLIENT only composes its badge from those fields.

The endpoint answer is authoritative: only an unreachable endpoint (DNS/socket/timeout)
falls back to the cache, while a TLS trust failure, a refused redirect, a non-200 status
or a malformed answer raises LicenseCheckError — an unverifiable answer must never look
like a valid license. An expired cache counts as not licensed, online or offline.

A private install's e-mail is confirmed by the web side through a one-time link;
confirmed -> private_licensed True. IP history lives on the web side. This is a visible
watermark and audit trail, not DRM — legal terms live in the repository LICENSE.
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
# NOTE: state lives in config/ — a top-level license/ dir collides with the LICENSE file on Windows
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
    """Read the local license state; missing/corrupt file -> fresh default, logged.

    Never fatal: the server must still boot.
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
    """Atomic write of the state file. Never raises; a failed write is logged because
    it leaves the offline cache un-updated."""
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
    """True when a licensed answer has passed its valid_until date (UTC).

    null/empty valid_until = perpetual; a malformed date fails closed.
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

    Raised instead of falling back to the cache, so the operator sees why (TLS trust,
    a refused redirect, HTTP status, malformed answer).
    """


# Network-level failures = "no endpoint answer" -> cache fallback OK; anything else is an
# unverified answer -> raise. A URLError wrapping an SSLError is a trust failure, not an outage.
_UNREACHABLE_ERRORS = (
    urllib.error.URLError,        # DNS / refused / connection reset (wraps socket errors)
    ConnectionError,              # socket-level, incl. reset
    TimeoutError,                 # urlopen timeout (Python 3.10+)
)


def _is_unreachable(exc: Exception) -> bool:
    """True when there is no endpoint answer at all (not an unverified one).

    HTTPError is never unreachable: a status arrived, so the endpoint answered.
    A URLError wrapping a socket/timeout failure is unreachable; one wrapping an
    SSLCertVerificationError is a trust failure -> not unreachable.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return not isinstance(exc.reason, ssl.SSLError)
    return isinstance(exc, _UNREACHABLE_ERRORS)


def _build_ssl_context() -> ssl.SSLContext:
    """TLS trust for the license endpoint: system CA bundle + hostname verification.

    No certificate pin: coolems.com uses a public CA-signed leaf cert that rotates every
    ~90 days, and chain-level pinning is not enforceable through ssl here (see
    app/providers/coolems/ssl_pinning.py, used for the self-signed relay link).
    """
    return ssl.create_default_context(ssl.Purpose.SERVER_AUTH)


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect on the license request.

    urllib would re-send the POST body in plaintext over HTTP and accept that plaintext
    200 as the license answer. Refusing keeps the answer TLS-verified and the registered
    e-mail encrypted; the failure surfaces as LicenseCheckError naming the target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LicenseCheckError(
            f"license endpoint redirected ({code}) {req.full_url} -> {newurl} — refusing "
            "to follow: the license answer must come from the configured endpoint over TLS "
            "(point COOLEMS_LICENSE_ENDPOINT at the real URL)"
        )


# One module-level opener: CA-verified HTTPS + no-redirect policy, decided once.
# urlopen(context=...) would rebuild an opener per call and still follow redirects.
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
    """One HTTPS round trip to the license endpoint.

    Raises LicenseCheckError on anything unverifiable (TLS trust, redirect, non-200,
    non-JSON/non-object answer); URLError/OSError mean unreachable, so the caller may
    use the cache fallback.
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
        # urlopen raises HTTPError for non-2xx before the status check above. The error
        # body is the only way to learn why, so read it best-effort and quote it.
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
        # Trust failure; also surfaces as URLError(reason=SSLError) — both raise, never cache.
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
    """Ask the license endpoint once and turn the answer into a server-side result.

    The cache is used only when the endpoint is unreachable (result then carries
    "offline": True); anything unverifiable raises LicenseCheckError.
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

    private without e-mail makes no network call; private with e-mail POSTs
    {mode:"private", email}; a company install POSTs {company, email} (GET without an
    e-mail, IP-only match). A successful answer is cached in last_check; a licensed cache
    past valid_until counts as not licensed, online or offline.

    Raises LicenseCheckError when the answer could not be verified. Returns
    {"status": "private" | "licensed" | "pending"}; "offline": True only on the cache
    fallback, "reason" carries not_registered / confirmation_pending / expired.
    """
    state = state if state is not None else load_state()

    if state["choice"] == "private" and not state.get("email"):
        # Private install without e-mail — fully offline, nothing to confirm.
        return {"status": "private"}

    if state["choice"] == "private":
        # Private WITH e-mail: the endpoint confirms it or answers confirmation_pending.
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

    Wire fields: use ("private" | "company"), company (name or None, company installs),
    email and private_licensed (private installs: True when the web side confirmed that
    e-mail and it is not expired). An expired or unconfirmed answer sends
    private_licensed False and no company name.
    """
    state = load_state()
    if state["choice"] == "private":
        cache = state.get("last_check") or {}
        # Confirmed by the web side and not expired -> private_licensed True (fail closed).
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

    The network call runs in a worker thread (to_thread) so the event loop is never
    blocked. Exits on CancelledError; other failures are logged, the loop keeps running.
    """
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
            # Not VERIFIED - operator-visible problem (TLS trust, HTTP status, bad answer),
            # not an outage, so no cache is used.
            logger.error("[SERVER] License check FAILED (not verified, no cache used): %s", e)
        except Exception as e:  # pragma: no cover - the loop itself must never die
            logger.error("[SERVER] License check loop error (non-fatal): %s: %s",
                         type(e).__name__, e)
        try:
            await _la.sleep(PERIODIC_CHECK_INTERVAL_SEC)
        except _la.CancelledError:
            raise


def start_license_task() -> "object | None":
    """Boot hook: start the check loop on the running event loop.

    Never blocks boot - license_loop() does the first check in a worker thread.
    Returns the task handle, or None when no loop is running.
    """
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
