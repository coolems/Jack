"""CLIENT UI TLS certificates (browser trust for the local UI URL, port from config.CLIENT_UI_PORT)."""

import os
import subprocess as _sp
import sys

from .paths import ROOT, _read_client_ui_port
from .ui import info, ok, warn


def _gen_ui_certs(certs_dir: str) -> bool:
    """Generate ca.crt + server.crt/server.key for the CLIENT UI into *certs_dir*.

    Uses the 'cryptography' package - installed on demand (one small pip install,
    no venv needed). Returns True on success. The CA is imported into the browser
    once and then the UI URL (https://127.0.0.1:<CLIENT_UI_PORT>/) is trusted automatically afterwards.
    """
    try:
        r = _sp.run([sys.executable, "-c", "import cryptography"], capture_output=True)
        if r.returncode != 0:
            info("Installing the 'cryptography' package (one small pip install)...")
            pr = _sp.run([sys.executable, "-m", "pip", "install", "--quiet", "cryptography"])
            if pr.returncode != 0:
                warn("'cryptography' could not be installed - skipping UI certificates.")
                return False
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gen_ui_certs.py")
        r2 = _sp.run([sys.executable, script, certs_dir], capture_output=True, text=True)
        if r2.returncode != 0:
            warn(f"UI certificate generation failed (exit {r2.returncode}) - skipping.")
            return False
        return True
    except Exception as e:
        warn(f"UI certificate generation problem ({e}) - skipping.")
        return False


def ensure_ui_certs(root: str) -> bool:
    """Make sure the CLIENT UI has TLS certs (fresh clones have none).

    Checks CLIENT/certs/ first, then <root>/certs/ - exactly the two locations
    entry/ssl_config.py looks at. If a pair is already there it is kept untouched;
    otherwise a local CA + leaf certificate are generated into CLIENT/certs/.
    Returns True when usable server.crt/server.key now exist (old or new).
    """
    client_certs = os.path.join(root, "CLIENT", "certs")
    root_certs = os.path.join(root, "certs")
    for d in (client_certs, root_certs):
        if os.path.isfile(os.path.join(d, "server.crt")) and os.path.isfile(os.path.join(d, "server.key")):
            ok(f"CLIENT UI certificates already present in {os.path.relpath(d, root)}\\ - kept untouched")
            return True

    info("No CLIENT UI certificates found (fresh clone) - generating a local CA + certificate...")
    made = _gen_ui_certs(client_certs)
    if not made:
        warn("CLIENT will start HTTP-only. You can re-run init later, or copy a server.crt/server.key "
             "pair into CLIENT\\certs\\ and restart the client.")
        return False

    ok(f"Generated CLIENT UI certificates in {os.path.relpath(client_certs, root)}\\ :")
    for f_ in ("ca.crt", "server.crt", "server.key"):
        fp = os.path.join(client_certs, f_)
        if os.path.isfile(fp):
            ok(f"  {fp}")

    print()
    info("One-time browser trust (do this ONCE per PC - the CA is valid for 10 years):")
    info(f"  1. Double-click {os.path.join(client_certs, 'ca.crt')} in File Explorer")
    info('     -> "Install Certificate" -> Local Machine -> "Trusted Root Certification Authorities".')
    info("  2. Restart the browser completely (close ALL windows).")
    info(f"     Afterwards https://127.0.0.1:{_read_client_ui_port()}/ is trusted automatically - no more warnings.")
    return True
