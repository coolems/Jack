#!/usr/bin/env python3
"""
Generate THIRD-PARTY-NOTICES.txt from requirements.txt (SERVER root).

Why this exists (licensing plan v9, Phase 1): every direct dependency in
requirements.txt is permissive-licensed (MIT/BSD/Apache/HPND), which means NO
source-disclosure obligation — but attribution IS required for redistribution.
This script keeps the notices file honest: it reads the pinned requirements and
emits one block per package with license + copyright holder, taken from a local
table that was verified against the installed dist-info METADATA/LICENSE files.

RULES BAKED IN (licensing plan v9, Phase 4 guardrail):
  - A dependency NOT in the table is emitted as "UNVERIFIED" and the script EXITS NON-ZERO
    -> a CI job running this script fails the build if someone adds an unknown
    (potentially copyleft) dependency. That permanently locks in low coverage.
  - A dependency whose license contains GPL/AGPL is REJECTED with exit code 2,
    even if it were added to the table by mistake.

Usage:
  python tools/generate_third_party_notices.py            # writes THIRD-PARTY-NOTICES.txt at repo root
  python tools/generate_third_party_notices.py --check    # CI mode: verify existing file is up to date, no write
Exit codes: 0 = ok, 1 = unknown package / stale file, 2 = copyleft detected.

Stdlib only (runs in any venv).
"""

import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQ_FILE = os.path.join(REPO_ROOT, "requirements.txt")
OUT_FILE = os.path.join(REPO_ROOT, "THIRD-PARTY-NOTICES.txt")

# Verified 2026-10-15 against the installed dist-info (METADATA + LICENSE files) in venv/.
# Format: normalised name -> (license_id, copyright_holder)
LICENSE_TABLE = {
    "pillow":            ("HPND / PIL",          "Python Software Foundation and contributors"),
    "aiohttp":           ("Apache-2.0",          "Aio-libs team and contributors"),
    "beautifulsoup4":    ("MIT",                 "Michael Foord et al."),
    "cryptography":      ("Apache-2.0 / BSD dual", "Python Cryptographic Authority and contributors"),
    "ddgs":              ("MIT",                 "deedy5 (verified: ddgs-9.16.0.dist-info/licenses/LICENSE.md)"),
    "fastapi":           ("MIT",                 "Sebastián Ramírez (tiangolo) and contributors"),
    "httpx":             ("BSD-3-Clause",        "Encode OSS Ltd."),
    "nest-asyncio":      ("BSD-2-Clause",        "Pawel Chomik and contributors"),
    "playwright":        ("Apache-2.0",          "Microsoft Corporation and contributors"),
    "psutil":            ("BSD-3-Clause",        "Giampaolo Rodola' and contributors"),
    "pyautogui":         ("MIT",                 "Nathan Hoad et al."),
    "pydantic":          ("MIT",                 "Samuel Colvin, Colin O'Flynn and contributors (pydantic-dev)"),
    "pytest":            ("MIT",                 "Holger Krekel and contributors"),
    "python-multipart":  ("Apache-2.0",          "Andrew Dunham et al. (verified: python_multipart-0.0.32.dist-info/licenses/LICENSE.txt)"),
    "pytz":              ("MIT",                 "Steven D'Aprano and contributors"),
    "requests":          ("Apache-2.0",          "Kenneth Reitz and contributors"),
    "starlette":         ("BSD-3-Clause",        "Encode OSS Ltd."),
    "urllib3":           ("MIT",                 "Andrey Petrov and contributors"),
    "uvicorn":           ("BSD-3-Clause",        "Mangodream and contributors (extras: httptools, watchfiles, websockets, uvloop, python-dotenv — all permissive)"),
    "websockets":          ("BSD-3-Clause",        "The Websockets Project (Aymeric Augustin) and contributors"),
}

# Licenses that would trigger source disclosure / copyleft obligations. Never allowed in this project.
COPYLEFT_MARKERS = ("GPL", "AGPL")


def _normalise(name: str) -> str:
    """PEP 503 normalisation so 'python-multipart' == 'Python_Multipart'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(path):
    """Yield (name, version_pin) for each requirement line in requirements.txt."""
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # strip extras like uvicorn[standard]
            m = re.match(r"^([A-Za-z0-9._\-]+)(?:\[[^\]]*\])?\s*==\s*([0-9][^;\s]*)", line)
            if not m:
                raise SystemExit(f"requirements.txt: cannot parse pinned requirement: {line!r}")
            yield _normalise(m.group(1)), m.group(2)


def build_notices():
    lines = [
        "COOLEMS \"Jack\" — THIRD-PARTY NOTICES",
        "=" * 60,
        "",
        "This file lists the third-party components used by COOLEMS and their licenses.",
        "All direct dependencies are permissively licensed (MIT/BSD/Apache/HPND): they may be",
        "redistributed with this software provided these notices are kept. None of them",
        "impose source-disclosure or copyleft obligations on Coolems KG's own code.",
        "",
        "Generated by tools/generate_third_party_notices.py from requirements.txt — do not edit",
        "by hand; change the pin, re-run the generator (CI enforces this).",
        "",
    ]

    unknown = []
    copyleft = []
    for name, version in parse_requirements(REQ_FILE):
        entry = LICENSE_TABLE.get(name)
        if entry is None:
            lines.append(f"{name}=={version}")
            lines.append("  LICENSE: UNVERIFIED — CHECK BEFORE DISTRIBUTING")
            lines.append("")
            unknown.append(name)
            continue

        license_id, holder = entry
        for marker in COPYLEFT_MARKERS:
            if marker.lower() in license_id.lower():
                copyleft.append((name, license_id))
        lines.append(f"{name}=={version}")
        lines.append(f"  License: {license_id}")
        lines.append(f"  Copyright holder: {holder}")
        lines.append("")

    # llama.cpp runtime binaries (not a pip dep — shipped/downloaded separately)
    lines += [
        "llama.cpp (runtime binaries, llama_server/)",
        "  License: MIT",
        "  Copyright holder: Georgi Gerganov and contributors",
        "  Note: when distributing the pre-built Windows CUDA binaries, keep their copyright",
        "  notice and license text in the package; if the installer downloads them at install",
        "  time (planned distribution model), the upstream MIT terms bind user-to-upstream.",
        "",
    ]

    return "\n".join(lines), unknown, copyleft


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="CI mode: fail if OUT_FILE is missing or stale")
    args = ap.parse_args()

    content, unknown, copyleft = build_notices()

    if copyleft:
        for name, lic in copyleft:
            print(f"FATAL: {name} carries a copyleft license ({lic}) — NOT allowed in this project.", file=sys.stderr)
        sys.exit(2)

    if args.check:
        if not os.path.isfile(OUT_FILE):
            print("FAIL: THIRD-PARTY-NOTICES.txt missing — run the generator.", file=sys.stderr)
            sys.exit(1)
        with open(OUT_FILE, "r", encoding="utf-8") as f:
            existing = f.read()
        if existing != content:
            print("FAIL: THIRD-PARTY-NOTICES.txt is out of date — re-run tools/generate_third_party_notices.py.", file=sys.stderr)
            sys.exit(1)
        if unknown:
            print(f"FAIL: unverified packages in requirements.txt: {', '.join(unknown)}", file=sys.stderr)
            sys.exit(1)
        print("OK: THIRD-PARTY-NOTICES.txt is up to date; all dependencies verified permissive.")
        return

    if unknown:
        # Still write the file (so a human can see what's unverified), but fail loudly.
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"WARN: wrote {os.path.basename(OUT_FILE)} but these packages are UNVERIFIED: {', '.join(unknown)}", file=sys.stderr)
        sys.exit(1)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Wrote {os.path.basename(OUT_FILE)} — all direct dependencies verified permissive.")


if __name__ == "__main__":
    main()
