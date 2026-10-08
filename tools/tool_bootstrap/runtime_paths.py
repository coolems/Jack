"""Runtime location resolution + venv helpers for self-unpacking tools.

<CLIENT_DIR> is published by code_client.py as the COOLEMS_CLIENT_ROOT environment variable
at startup; tool runtimes are INTERNAL to the client (<CLIENT_DIR>/tools/runtimes/<name>)
and never live in the user's working folder. Without that variable (pure SERVER process /
local dev) the legacy location <working_root>/tools/<runtime_name> is used instead - and a
runtime found there is migrated ONCE to the client location on first use after an upgrade
(the moved venv is smoke-tested; if broken it is removed and rebuilt fresh).
"""

import logging
import os
import shutil
import subprocess


logger = logging.getLogger("COOLEMS.Tools.Bootstrap")

def _get_working_root() -> str:
    """Live working root - single source of truth is <CLIENT>/config/.working_root.json."""
    from tools.utils import get_working_root

    return get_working_root()


def _client_tools_root() -> str | None:
    """Client-internal runtimes root, or None when not running inside the CLIENT process.

    code_client.py publishes its own directory as COOLEMS_CLIENT_ROOT at startup; tool
    runtimes are INTERNAL to the client and must never clutter the user's working folder.
    """
    client_root = os.environ.get("COOLEMS_CLIENT_ROOT", "").strip()
    if not client_root:
        return None
    return os.path.normpath(os.path.join(client_root, "tools", "runtimes"))


def resolve_runtime_dir(runtime_name: str) -> str:
    """Absolute path of this tool's own runtime folder.

    CLIENT process (COOLEMS_CLIENT_ROOT set): <CLIENT_DIR>/tools/runtimes/<runtime_name>
    - internal to the client, independent of the working folder. Otherwise (pure server
    process / local dev) the legacy location: <working_root>/tools/<runtime_name>.
    """
    if not isinstance(runtime_name, str) or "/" in runtime_name or "\\" in runtime_name \
            or ".." in runtime_name or not runtime_name.strip():
        raise ValueError(f"Invalid runtime name: {runtime_name!r}")
    client_root = _client_tools_root()
    base_dir = client_root if client_root else os.path.join(_get_working_root(), "tools")
    return os.path.normpath(os.path.join(base_dir, runtime_name))


def _smoke_test_venv(venv_py: str) -> bool:
    """True when the (possibly relocated) venv python still starts and imports sys.

    NOTE: must use "-c import sys" - a BARE python.exe enters interactive mode and
    waits on stdin forever, which would look like a broken venv (60s timeout).
    """
    try:
        r = subprocess.run([venv_py, "-c", "import sys"], capture_output=True,
                           text=True, timeout=60)
        return r.returncode == 0
    except PermissionError:
        # POSIX: a relocated venv may have lost its exec bit - restore and retry once.
        try:
            os.chmod(venv_py, 0o755)
        except Exception:
            return False
        try:
            r = subprocess.run([venv_py, "-c", "import sys"], capture_output=True,
                               text=True, timeout=60)
            return r.returncode == 0
        except Exception:
            return False
    except Exception as e:
        logger.warning(f"[bootstrap] venv smoke test failed to run: {e}")
        return False


def _migrate_legacy_runtime(runtime_dir: str) -> None:
    """One-time move of a pre-upgrade runtime from the legacy working-root location.

    Only runs when COOLEMS_CLIENT_ROOT is set, the new location is empty and the legacy
    folder still holds a real runtime (venv python or deps marker). The moved venv is
    smoke-tested; on failure it is removed so the normal fresh-create path rebuilds it.
    """
    client_root = _client_tools_root()
    if not client_root:
        return  # server-side dev: legacy location IS canonical here - nothing to migrate
    if os.path.exists(runtime_dir):
        return  # already at the new home (or partially created) - leave it alone
    legacy_dir = os.path.normpath(os.path.join(_get_working_root(), "tools",
                                               os.path.basename(runtime_dir)))
    if not os.path.isdir(legacy_dir) or legacy_dir == runtime_dir:
        return  # only stray files - nothing worth migrating; let the fresh path run

    has_venv_py = os.path.isfile(_venv_python(legacy_dir))
    has_marker = os.path.isfile(os.path.join(legacy_dir, ".deps_installed"))
    if not (has_venv_py or has_marker):
        return  # only stray files - nothing worth migrating; let the fresh path run

    logger.info(f"[bootstrap] Migrating legacy tool runtime {legacy_dir} -> {runtime_dir}")
    os.makedirs(os.path.dirname(runtime_dir), exist_ok=True)
    try:
        shutil.move(legacy_dir, runtime_dir)
    except Exception as e:
        # Partial move cleanup so the next call can retry from a clean slate.
        if os.path.isdir(runtime_dir):
            shutil.rmtree(runtime_dir, ignore_errors=True)
        raise RuntimeError(f"Legacy runtime migration failed ({e}); "
                           f"a fresh runtime will be built instead") from e

    venv_py = _venv_python(runtime_dir)
    if has_venv_py and os.path.isfile(venv_py) and not _smoke_test_venv(venv_py):
        logger.warning("[bootstrap] Migrated venv failed its smoke test - removing it; "
                       "a fresh venv will be created (one-time reinstall)")
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _venv_python(runtime_dir: str) -> str:
    """Path of the venv's python executable (Windows + POSIX layouts)."""
    if os.name == "nt":
        return os.path.join(runtime_dir, "venv", "Scripts", "python.exe")
    return os.path.join(runtime_dir, "venv", "bin", "python")


def _run_checked(cmd: list, timeout: int, label: str) -> None:
    """Run *cmd*, raising a clean RuntimeError with the tail of stderr on failure."""
    logger.info(f"[bootstrap] {label}: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[-500:]
        raise RuntimeError(f"{label} failed (exit {r.returncode}): {detail}")
