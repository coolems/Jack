"""ripgrep runtime resolution for the search tools - NOT a tool itself.

Finds a hash-verified rg binary in this tool's runtime folder, unpacking the pinned npm
tarball on demand. Same convention as generate_image: SERVER ships only a manifest
(version, URL, sha256), the heavy file lands on the user machine in
<CLIENT_DIR>/tools/runtimes/search_tool/ - never in the user's working folder, never in
git. The init phase (utils/init_setup/ripgrep.py) normally places it there already; this
module is the fallback for machines where init skipped it or the file got corrupted.

Import policy: stdlib only, nothing from ast_guard.DANGEROUS_MODULES - urllib/tarfile/
hashlib are all outside that blocklist, so this module needs NO policy.py rows. The
binary is verified BEFORE it is ever executed: tarball hash, extracted size, binary hash.
"""

import hashlib
import logging
import os
import tarfile
import urllib.error
import urllib.request

from ..tool_bootstrap import resolve_runtime_dir

logger = logging.getLogger("COOLEMS.Tools.Search")

RUNTIME_NAME = "search_tool"
BINARY_NAME = "rg.exe" if os.name == "nt" else "rg"

# Fallback pins: used only when a legacy SERVER ships no TOOL_MANIFEST at all.
# Mirrors tools/search_tools/tool_manifest.json and utils/init_setup/constants.py.
DEFAULT_MANIFEST = {
    "runtime_name": RUNTIME_NAME,
    "tarball_url": "https://registry.npmjs.org/@vscode/ripgrep-win32-x64/-/ripgrep-win32-x64-1.18.0.tgz",
    "tarball_size": 2_111_160,
    "tarball_sha256": "725006b9662fa0f73cb0dc7d62b666524b80cfe125214ca75dab5293ef9e4203",
    "tarball_member": "package/bin/rg.exe",
    "binary_size": 5_429_760,
    "binary_sha256": "f9dde63498b3193f098355dbec97af99dc4f6b8fa0df5ed04114a03012c042cb",
}


def active_manifest() -> dict:
    """The SERVER-shipped manifest when present, else the in-file pins.

    TOOL_MANIFEST is injected as a global by the CLIENT loader (compile_tool step 6b);
    globals() is read instead of an import so the tool's own namespace is consulted.
    """
    m = globals().get("TOOL_MANIFEST")  # noqa: B009 - injected symbol by design
    if isinstance(m, dict) and m.get("tarball_url") and m.get("binary_sha256"):
        return m
    return DEFAULT_MANIFEST


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unpack_tarball(url: str, dest_dir: str, manifest: dict) -> str:
    """Download the pinned tarball, verify hashes, extract the binary. Returns its path.

    Raises RuntimeError with a human message on any failure; an unverified executable is
    never left in the runtime folder (it is removed before raising).
    """
    exe = os.path.join(dest_dir, BINARY_NAME)
    os.makedirs(dest_dir, exist_ok=True)
    tgz = os.path.join(dest_dir, "ripgrep.tarball.tgz")

    req = urllib.request.Request(url, headers={"User-Agent": "coolems-search/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(tgz, "wb") as out:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"ripgrep download failed ({e}). URL: {url}") from e

    want_size = int(manifest.get("tarball_size") or 0)
    if want_size and os.path.getsize(tgz) != want_size:
        os.remove(tgz)
        raise RuntimeError(f"ripgrep tarball is {os.path.getsize(tgz)} bytes, "
                           f"expected {want_size} - not the pinned build.")
    if _sha256_of(tgz) != manifest["tarball_sha256"]:
        os.remove(tgz)
        raise RuntimeError("ripgrep tarball sha256 mismatch - nothing was installed.")

    member = manifest.get("tarball_member") or "package/bin/rg.exe"
    try:
        with tarfile.open(tgz, "r:gz") as tf:
            try:
                item = tf.getmember(member)
            except KeyError:
                raise RuntimeError(f"tarball does not contain {member} - unexpected layout.")
            if item.isdir():   # tarfile's TarInfo method name (no is_dir())
                raise RuntimeError(f"{member} is a directory inside the tarball.")
            src = tf.extractfile(item)
            written = 0
            with open(exe, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
    finally:
        try:
            os.remove(tgz)
        except OSError:
            pass

    want_bin = int(manifest.get("binary_size") or 0)
    if want_bin and written != want_bin:
        os.remove(exe)
        raise RuntimeError(f"ripgrep extracted {written} bytes, expected {want_bin}.")
    if _sha256_of(exe) != manifest["binary_sha256"]:
        os.remove(exe)
        raise RuntimeError("ripgrep sha256 mismatch - the binary was removed and not used.")
    return exe


def ensure_binary(manifest: dict | None = None) -> str:
    """Path of a hash-verified rg binary in this tool's runtime folder (idempotent).

    A file matching the pinned size + sha256 is reused with no network work; a missing
    or wrong-hash file is re-unpacked from the pinned URL.
    """
    manifest = manifest or active_manifest()
    dest_dir = resolve_runtime_dir(manifest.get("runtime_name") or RUNTIME_NAME)
    exe = os.path.join(dest_dir, BINARY_NAME)

    if os.path.isfile(exe):
        if os.path.getsize(exe) == int(manifest.get("binary_size") or 0) \
                and _sha256_of(exe) == manifest["binary_sha256"]:
            return exe
        logger.warning("[search] rg.exe at %s is not the pinned build - reinstalling", exe)
        os.remove(exe)

    exe = _unpack_tarball(manifest["tarball_url"], dest_dir, manifest)
    logger.info("[search] ripgrep unpacked into %s", exe)
    return exe
