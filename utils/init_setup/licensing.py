"""LICENSING STEP for ZZZ_initial_init.bat (console, stdlib-only).

LICENSING PLAN v14 (2026-10-15): the CLIENT keeps NO license files and never talks to
coolems.com. ALL license logic lives on the SERVER: it reads its own state file
(<SERVER ROOT>/config/license_state.json), checks coolems.com at boot + every 30 min,
writes the answer back into that same file (offline fallback when unreachable; an
expired cache is treated as not licensed), and feeds a slim {use, company} payload to
every CLIENT over WebSocket (auth_ok + each token_stats frame). The CLIENT badge only
DISPLAYS what it was fed.

IP history of check attempts lives on the WEB side now — index.php writes
ip_history.json next to licenses.json. This local file stores NO ip data.

This init step therefore:
  - asks "private or company?";
  - WRITES the answer into <SERVER ROOT>/config/license_state.json with ALL user input
    (choice, company name, e-mail, seats wanted, free-text request) — the server reads
    exactly this file on boot;
  - private  -> asks for an e-mail (optional). With e-mail: the server asks coolems.com
                at boot + every 30 min; the web side sends a one-time confirmation link and
                only writes the address into its own private_license.json after it is clicked.
                Without e-mail: fully offline, zero friction (server sends {use:"private"}).
  - company  -> prints + opens a pre-filled license-request e-mail. The server then
                fetches confirmation from coolems.com itself at boot and caches the
                answer in the same file — no manual editing needed anywhere.

STDLIB-ONLY at import time (init runs before any venv exists) — so it writes the JSON
directly instead of importing app/license_server.py; the schema is kept identical to
app/license_server.default_state().
"""

import json
import os
import re as _re
import sys
from datetime import datetime, timezone

from .paths import ROOT
from .ui import ask, info, ok, warn


CONTACT_EMAIL = "coolems.kg@gmail.com"

# Server-side license state file — the ONLY license file that exists on this machine.
# NOTE: state lives in config/ (a top-level license/ dir is impossible on Windows — it collides case-insensitively with the root LICENSE file)
LICENSE_DIR = os.path.join(ROOT, "config")
STATE_FILE = os.path.join(LICENSE_DIR, "license_state.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def default_state() -> dict:
    """Identical schema to app/license_server.default_state()."""
    return {
        "choice": None,            # "private" | "company" | None (not asked yet)
        "company": "",
        "email": "",
        "users_requested": 0,      # informational only — never sent to the endpoint
        "request_text": "",        # free text from registration; DATA ONLY: HTML-escape on render
        "created_at": _now_iso(),
        "last_check": None,        # full coolems.com answer + checked_at — written by the server
    }


def load_state() -> dict:
    """Read existing state (re-run of init) or a fresh default. Never fatal."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_state()
        base = default_state()
        for key in base:
            if key in data:
                base[key] = data[key]
        if not isinstance(base.get("last_check"), dict):
            base["last_check"] = None
        return base
    except (OSError, json.JSONDecodeError):
        return default_state()


def save_state(state: dict) -> bool:
    """Atomic write of the license state file. Returns True on success."""
    try:
        os.makedirs(LICENSE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
        try:
            os.replace(tmp, STATE_FILE)
        except OSError:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
            os.rename(tmp, STATE_FILE)
        return True
    except OSError as e:
        warn(f"Could not write {STATE_FILE}: {e}")
        return False


def build_mail_body(company: str, email: str, users_requested: int, request_text: str):
    """Return (subject, body) of the license-request e-mail."""
    subject = f"COOLEMS commercial license — {company}"
    lines = [
        "We would like a commercial license for COOLEMS.",

        f"Company: {company}",
        f"Contact e-mail: {email or '(not provided)'}",
        f"Users/seats requested: {users_requested}",

        "Request:",
        request_text or "(no additional details)",
    ]
    return subject, "\n".join(lines)


def print_composed_mail(company: str, email: str, users_requested: int, request_text: str) -> None:
    """Print the full composed message so it can be sent manually from any inbox."""
    import urllib.parse

    subject, body = build_mail_body(company, email, users_requested, request_text)
    mailto = "mailto:" + CONTACT_EMAIL + "?" + urllib.parse.urlencode({"subject": subject, "body": body})

    print()
    info("Send this e-mail from YOUR OWN inbox to activate the commercial license:")
    print()
    print(f"      TO:      {CONTACT_EMAIL}")
    print(f"      SUBJECT: {subject}")
    print()
    for ln in body.splitlines():
        print(f"      {ln}")
    print()
    info("One-click link (opens your default mail client pre-filled):")
    print(f"      {mailto}")
    print()

    # Try to open the default mail client automatically — best effort, never fatal.
    try:
        if sys.platform.startswith("win"):
            os.startfile(mailto)  # type: ignore[attr-defined]
            ok("Your e-mail client should have opened with the message pre-filled.")
        elif sys.platform == "darwin":
            os.system(f'open "{mailto}"')
            ok("Your e-mail client should have opened with the message pre-filled.")
        else:
            os.system(f'xdg-open "{mailto}"')
            ok("Your e-mail client should have opened with the message pre-filled.")
    except Exception as e:
        warn(f"Could not open your e-mail client automatically ({e}).")
        info("Copy the TO/SUBJECT/body printed above and send it from any mail program you like.")


def run_licensing_step() -> dict:
    """Console licensing flow. Asks, then WRITES <ROOT>/config/license_state.json.

    Returns a small summary dict for the final report."""
    print()
    info("COOLEMS Jack is free for individual use.")
    info("Companies commercial use requires a written license from Coolems KG.")
    print()
    while True:
        val = ask(
            "How will you use COOLEMS?\n"
            "      [P] Private (individual) - free (incl. commercial use)\n"
            "      [C] Company / commercial use",
            default="P").strip().lower()
        if val in ("p", "private"):
            choice = "private"
            break
        if val in ("c", "company", "commercial"):
            choice = "company"
            break
        warn("Please enter P (private) or C (company)")

    state = load_state()   # re-run of init: keep last_check the server wrote

    # ---- PRIVATE ---------------------------------------------------------------
    if choice == "private":
        print()
        info("Private License — including commercial use as an individual.")
        info("Enter your e-mail so we can confirm it (one-time link, sent by coolems.com).")
        email = ""
        while True:
            raw_email = ask("Your e-mail (will be used for license confirmation)", default="").strip().lower()
            if not raw_email or _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", raw_email):
                email = raw_email
                break
            warn("That does not look like a valid e-mail — try again")

        state.update({"choice": "private", "company": "", "email": email})
        saved = save_state(state)
        if saved:
            ok("Recorded: PRIVATE USE.")
            info(f"Written to {STATE_FILE} — the server will send 'PRIVATE USE' to all clients.")
        if email:
            info("The SERVER will ask coolems.com to e-mail you a one-time confirmation link;")
            info("click it once and your address is registered in private_license.json.")
        else:
            ok("No e-mail — running fully offline, nothing collected. Done.")
        return {"choice": "private", "email": email}

    # ---- COMPANY ---------------------------------------------------------------
    print()
    info("You are registering a COMPANY / COMMERCIAL use of COOLEMS.")
    company = ""
    while not company:
        company = ask("Company name * (e.g. Coco SRL)").strip()
        if not company:
            warn("Company name is required")
    email = ask("Contact e-mail (you@company.com)", default="").strip().lower()
    users = 0
    while users < 1:
        raw = ask("Number of users / seats wanted * (e.g. 5)").strip()
        try:
            users = int(raw)
        except ValueError:
            users = 0
        if users < 1:
            warn("Enter a whole number >= 1")
    request_text = ask(
        "General license request — what do you want to use COOLEMS for, scope, questions\n"
        "(optional, free text)", default="").strip()

    state.update({
        "choice": "company",
        "company": company,
        "email": email,
        "users_requested": users,
        "request_text": request_text[:4000],   # bound the free text (same rule as server)
    })
    if not state.get("created_at"):
        state["created_at"] = _now_iso()

    saved = save_state(state)
    if saved:
        ok(f"Registration written to {STATE_FILE}")
        info("The SERVER reads this file on boot and asks coolems.com itself for license")
        info("confirmation (immediately + every 30 min), then feeds the client badge —")
        info("no manual editing needed.")

    # Compose the e-mail: try one-click open AND always print the full message.
    print_composed_mail(company, email, users, request_text)

    return {"choice": "company", "company": company}
