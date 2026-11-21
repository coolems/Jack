"""Auto-install of optional addon dependencies (2026-07).

An addon engine imports its package lazily. When the package is missing from the
CLIENT venv, the registry catches the ImportError, installs the pinned packages
into the running interpreter (sys.executable -m pip install), and retries the
load once - so enabling an addon in Settings just works, no manual pip needed.

Pins mirror CLIENT/requirements.txt (the "Voice addons" block). Keep them in sync.
"""

import logging
import subprocess
import sys
from typing import Dict, List

logger = logging.getLogger("COOLEMS")

#: provider -> the exact pinned packages it needs (pip spec, already installed = no-op)
PROVIDER_DEPS: Dict[str, List[str]] = {
    "supertonic": ["supertonic==1.3.1", "soundfile==0.14.0"],
    "faster_whisper": ["faster-whisper==1.2.1", "ctranslate2==4.9.0", "av==19.0.2"],
    # OpenAI providers need only the stdlib/requests stack - nothing to install.
    "openai_tts": [],
    "openai_stt": [],
}


def deps_for(provider: str) -> List[str]:
    """Pinned pip specs for one provider (empty = nothing to install)."""
    return list(PROVIDER_DEPS.get(provider, []))


def install(specs: List[str]) -> None:
    """Install pinned packages into the running interpreter. Blocking - call in an executor.

    Raises RuntimeError with the pip stderr tail when the install fails."""
    if not specs:
        return
    cmd = [sys.executable, "-m", "pip", "install",
           "--disable-pip-version-check", "--quiet", *specs]
    logger.info("[Addons] auto-installing: %s", " ".join(specs))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError("pip install failed: " + " | ".join(tail))
    logger.info("[Addons] auto-install done: %s", " ".join(specs))
