"""Integrity verification for SERVER-delivered source code (fail-closed).

Both sides of the wire use the SAME scheme -- first 16 hex chars of SHA-256:

    sha256(source.encode('utf-8')).hexdigest()[:16]

The SERVER computes it in app/providers/coolems/tool_scanner._sha_prefix and ships
it alongside every shared module ('shared_hashes') and tool source
('tool_code_response.source_hash'). The CLIENT verifies BEFORE exec/compile:

  * a promised hash that does NOT match => the code is REFUSED (fail-closed,
    logged with 'REFUSING' so tests/guards can detect the enforcement path);
  * no promised hash (legacy server)   => tolerated and installed.

tests/test_protocol_sync_20260820.py pins this scheme end-to-end on both sides,
so changing the digest or its length here would break that guard on purpose.
"""

import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def sha_prefix(source_code: str, length: int = 16) -> str:
    """First *length* hex chars of the SHA-256 digest of *source_code*."""
    return hashlib.sha256(source_code.encode('utf-8')).hexdigest()[:length]


class IntegrityCheckResult:
    """Outcome of a fail-closed hash verification (small, explicit, log-friendly)."""

    __slots__ = ("ok", "expected", "actual")

    def __init__(self, ok: bool, expected: Optional[str], actual: str):
        self.ok = ok
        self.expected = expected
        self.actual = actual


def verify_shared_module_hash(module_name: str, source_code: str,
                              expected_hash: Optional[str]) -> IntegrityCheckResult:
    """Verify one delivered SHARED/framework module against its promised hash.

    Fail-closed (2026-08-20): when the SERVER shipped a hash for this module and
    the bytes do not match, *ok* is False -- the caller must skip exec entirely.
    An absent hash means legacy server: tolerated (ok=True).
    """
    actual = sha_prefix(source_code)
    if expected_hash is None:
        logger.debug("Loader: no integrity hash promised for shared module '%s' (actual=%s)",
                     module_name, actual)
        return IntegrityCheckResult(True, None, actual)

    if actual == expected_hash:
        logger.debug("Loader: hash verified for shared module '%s' (%s)", module_name, actual)
        return IntegrityCheckResult(True, expected_hash, actual)

    logger.error(
        "Loader: HASH MISMATCH for shared module '%s': server=%s, actual=%s -- REFUSING to exec (fail-closed)",
        module_name, expected_hash, actual,
    )
    return IntegrityCheckResult(False, expected_hash, actual)


def verify_tool_hash(tool_name: str, source_code: str,
                     expected_hash: Optional[str]) -> IntegrityCheckResult:
    """Verify one delivered TOOL source against the hash shipped in tool_code_response.

    Same fail-closed contract as :func:`verify_shared_module_hash`, but for a single
    tool's source (the message carries 'source_hash' for exactly this file).
    """
    actual = sha_prefix(source_code)
    if expected_hash is None:
        logger.debug("Loader: no integrity hash promised for tool '%s' (actual=%s)",
                     tool_name, actual)
        return IntegrityCheckResult(True, None, actual)

    if actual == expected_hash:
        logger.debug("Loader: hash verified for tool '%s' (%s)", tool_name, actual)
        return IntegrityCheckResult(True, expected_hash, actual)

    logger.error(
        "Loader: HASH MISMATCH for tool '%s': server=%s, actual=%s -- REFUSING to compile (fail-closed)",
        tool_name, expected_hash, actual,
    )
    return IntegrityCheckResult(False, expected_hash, actual)


def hash_in_allowlist(source_code: str, allowed_hashes: set[str]) -> bool:
    """True when the source's prefix is in *allowed_hashes* (or no allowlist is active).

    Legacy advisory path kept from the original loader: some deployments configure a
    static allowlist of known-good prefixes. Empty allowlist == allow everything.
    """
    if not allowed_hashes:
        return True
    actual = sha_prefix(source_code)
    if actual in allowed_hashes:
        return True
    logger.warning(
        "Loader: hash NOT allowed for source: actual=%s, allowed=%s",
        actual, sorted(allowed_hashes),
    )
    return False
