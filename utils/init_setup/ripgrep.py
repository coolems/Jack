"""ripgrep binary for search_tools (grep + glob_files) - pinned npm package install.

The binary is NOT part of the tool payload: SERVER ships only a manifest (version, URL,
sha256), and the file itself lands here at init time (or, if init skipped it, when the
tool self-unpacks on first use). Both paths write to the same runtime folder, so the
tool's resolver does not care which one produced the file.

Hash verification happens BEFORE the binary is ever executed: the tarball is checked
against RG_TARBALL_SHA256, the extracted rg.exe against RG_BINARY_SHA256, and only then
is `rg --version` run as a self-test. A mismatch is reported loudly - an unverified
executable is never run.

Stdlib-only (init runs before any venv exists) and no config imports - see paths.py.
"""

import json
import os
import shutil
import subprocess

from .constants import (RG_BINARY_SHA256, RG_BINARY_SIZE, RG_MEMBER, RG_PKG, RG_RUNTIME_NAME,
                        RG_TARBALL_SHA256, RG_TARBALL_SIZE, RG_TARBALL_URL, RG_VERSION)
from .downloads import DownloadBlockedBySsl, download_file
from .errors import InitError
from .paths import ROOT
from .tarball import extract_member, sha256_of
from .ui import human, info, ok, warn, write_json

MANIFEST_NAME = ".rg_manifest.json"
SELF_TEST_ARGS = ["--no-config", "--version"]


def runtime_dirs() -> list[str]:
    """Folders where a rg.exe is usable, primary first.

    CLIENT process resolves its runtime as <CLIENT_DIR>/tools/runtimes/<name> (it
    publishes COOLEMS_CLIENT_ROOT at startup); a pure SERVER/dev process uses the legacy
    <repo>/tools/<name>. Init writes the primary and mirrors it to the legacy folder so
    both processes find the same verified binary.
    """
    client_dir = os.path.join(ROOT, "CLIENT", "tools", "runtimes", RG_RUNTIME_NAME)
    legacy = os.path.join(ROOT, "tools", RG_RUNTIME_NAME)
    return [client_dir] if os.path.normpath(client_dir) == os.path.normpath(legacy) else [client_dir, legacy]


def _self_test(exe: str) -> str:
    """Run `rg --version`; returns the version line. Raises InitError on any failure."""
    try:
        r = subprocess.run([exe, *SELF_TEST_ARGS], capture_output=True, text=True, timeout=30)
    except OSError as e:
        raise InitError(f"ripgrep could not be executed ({e}) - the binary is not runnable here.") from e
    if r.returncode != 0:
        raise InitError(f"ripgrep self-test failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:200]}")
    return (r.stdout or "").strip().splitlines()[0] if (r.stdout or "").strip() else "ripgrep"


def _install_one(tgz_path: str, dest_dir: str) -> str:
    """Extract rg.exe into *dest_dir*, verify hashes, self-test. Returns the exe path."""
    exe = os.path.join(dest_dir, "rg.exe")
    written = extract_member(tgz_path, RG_MEMBER, exe)
    if written != RG_BINARY_SIZE:
        os.remove(exe)
        raise InitError(f"ripgrep extracted {written} bytes but the known-good size is "
                        f"{RG_BINARY_SIZE} - the binary was NOT installed.")
    got = sha256_of(exe)
    if got != RG_BINARY_SHA256:
        os.remove(exe)
        raise InitError(
            f"ripgrep sha256 mismatch (got {got}, expected {RG_BINARY_SHA256}).\n"
            "  The binary was removed and NOT executed - re-run init to retry."
        )
    version = _self_test(exe)
    write_json(os.path.join(dest_dir, MANIFEST_NAME), {
        "package": RG_PKG,
        "package_version": RG_VERSION,
        "ripgrep_version": version,
        "sha256": got,
        "size": written,
        "source": "init",
    })
    return exe


def install_ripgrep() -> None:
    """Idempotent install of rg.exe into the search_tools runtime folder(s).

    A file whose sha256 already matches the pin is left alone (nothing re-downloaded);
    a wrong-size or wrong-hash file is replaced. Offline / SSL failures are a WARNING,
    not an abort: the tool self-unpacks on first use, so init must not block the setup.
    """
    dirs = runtime_dirs()
    primary = dirs[0]
    exe = os.path.join(primary, "rg.exe")

    if os.path.isfile(exe):
        if os.path.getsize(exe) == RG_BINARY_SIZE and sha256_of(exe) == RG_BINARY_SHA256:
            version = _self_test(exe)
            ok(f"ripgrep already installed and verified ({human(RG_BINARY_SIZE)}) - {version}")
            _mirror_missing(exe, dirs)
            return
        warn(f"Existing rg.exe is not the pinned build (size {os.path.getsize(exe)}) - reinstalling")
        os.remove(exe)

    temp_dir = os.path.join(ROOT, ".temp")
    os.makedirs(temp_dir, exist_ok=True)
    # npm's package name contains '/' (the scope) - unusable as a Windows file name, so the
    # local copy is named without it. download_file() resumes via <dest>.part, which needs a
    # plain filename too.
    tgz_path = os.path.join(temp_dir, f"ripgrep-win32-x64-{RG_VERSION}.tgz")

    info(f"Downloading ripgrep {RG_VERSION} ({RG_PKG}, ~{human(RG_TARBALL_SIZE)})...")
    try:
        download_file(RG_TARBALL_URL, tgz_path, expected_size=RG_TARBALL_SIZE,
                      label=os.path.basename(tgz_path))
    except (DownloadBlockedBySsl, InitError) as e:
        warn(str(e))
        warn("ripgrep NOT installed by init - the grep/glob_files tools will fetch it themselves "
             "on first use (same pinned URL + sha256). Everything else in init is unaffected.")
        info(f"Manual alternative: download {RG_TARBALL_URL} and extract {RG_MEMBER} into {primary}")
        return

    tarball_hash = sha256_of(tgz_path)
    if tarball_hash != RG_TARBALL_SHA256:
        os.remove(tgz_path)
        raise InitError(
            f"ripgrep tarball sha256 mismatch (got {tarball_hash}, expected {RG_TARBALL_SHA256}).\n"
            "  Nothing was installed - the downloaded package is not the pinned build."
        )

    exe = _install_one(tgz_path, primary)
    try:
        os.remove(tgz_path)                      # reclaim the ~2 MB tarball once extracted
    except OSError:
        pass
    version = _self_test(exe)
    ok(f"ripgrep installed: {exe} ({human(RG_BINARY_SIZE)}) - {version}")
    _mirror_missing(exe, dirs)


def _mirror_missing(exe: str, dirs: list[str]) -> None:
    """Copy the verified binary to the legacy runtime folder when it is missing there.

    A plain copy, NOT a hardlink: a hardlink makes the mirror the SAME inode as the
    primary, so a later corrupt rewrite of either file (a bad download, a test fixture)
    would silently destroy the other one. Duplicating 5.2 MB is cheap; sharing an inode is
    a correctness hazard. A mirror whose hash does not match is replaced.
    """
    for d in dirs[1:]:
        target = os.path.join(d, "rg.exe")
        if os.path.isfile(target) and sha256_of(target) == RG_BINARY_SHA256:
            continue
        os.makedirs(d, exist_ok=True)
        if os.path.isfile(target):
            os.remove(target)
        with open(exe, "rb") as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        write_json(os.path.join(d, MANIFEST_NAME), {
            "package": RG_PKG, "package_version": RG_VERSION,
            "sha256": RG_BINARY_SHA256, "size": RG_BINARY_SIZE, "source": "init-mirror",
        })
