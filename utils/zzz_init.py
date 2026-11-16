#!/usr/bin/env python3
"""ZZZ initial init - makes a FRESH GITHUB CLONE of this repo fully runnable on THIS machine.
Entry point: ZZZ_initial_init.bat (repo root) checks for Python >= 3.10 and hands off here.
This script is STDLIB-ONLY (no pip packages, no venv needed) so it runs before anything else
exists.
"""

import os
import sys

# Ensure the repo ROOT is on sys.path so `utils.init_setup` resolves no matter where this
# script is launched from (e.g. ZZZ_initial_init.bat runs it as a plain file, which puts
# utils/ - not the repo root - first on sys.path).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # <repo>/utils/zzz_init.py -> <repo>
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.init_setup.main import main  # noqa: F401  (re-exported for `python -m`-style use)

if __name__ == "__main__":
    sys.exit(main())
