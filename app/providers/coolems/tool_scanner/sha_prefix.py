"""SHA-256 prefix hashing for delivered-code integrity verification."""

import hashlib

def _sha_prefix(source: str, length: int = 16) -> str:
    """First *length* hex chars of the SHA-256 of a source string.

    Used for INTEGRITY VERIFICATION on the CLIENT (fail-closed): the client
    refuses to exec delivered code whose hash does not match what the SERVER
    shipped alongside it. This is defense-in-depth over the TLS channel --
    with hashes present, tampering in transit or by a compromised relay is
    detectable and blocked instead of merely warned about (2026-08-20).
    """
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:length]
