"""Tarball helpers for init - sha256 verification + single-member extraction.

npm publishes tarballs (.tgz = gzip tar), not zips, so downloads.extract_flat() cannot be
used for them. Split out of downloads.py so each concern stays readable alone (the same
reason utils/init_setup is a package): downloads.py owns the network engine, this module
owns the archive side. Stdlib-only - init runs before any venv exists.
"""

import hashlib
import os
import tarfile

from .errors import InitError


def sha256_of(path: str) -> str:
    """Hex sha256 of a file, streamed in 1 MiB chunks (safe for multi-GB files)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_member(tgz_path: str, member: str, dest_path: str) -> int:
    """Extract ONE member from a gzip tar (.tgz) to *dest_path*. Returns bytes written.

    The member path is validated (no '..', no absolute path) before anything is written.
    A missing member means the downloaded package is not the expected build - reported as
    InitError rather than a KeyError traceback.
    """
    with tarfile.open(tgz_path, "r:gz") as tf:
        try:
            item = tf.getmember(member)
        except KeyError:
            raise InitError(
                f"{os.path.basename(tgz_path)} does not contain {member} - "
                "the downloaded package is not the expected one."
            )
        if item.isdir():   # tarfile's TarInfo method name (no is_dir())
            raise InitError(f"{member} is a directory inside the tarball - expected a file.")
        parts = [p for p in member.replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p.startswith("..") or os.path.isabs(p) for p in parts):
            raise InitError(f"Refusing to extract unsafe tarball member: {member}")
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        src = tf.extractfile(item)
        if src is None:
            raise InitError(f"Could not read {member} from {os.path.basename(tgz_path)}.")
        written = 0
        with open(dest_path, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
    return written
