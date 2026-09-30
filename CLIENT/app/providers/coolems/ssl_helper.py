"""SSL context builders for CoolemsClientProvider."""

import logging
import os
import ssl as ssl_mod

logger = logging.getLogger("COOLEMS.Provider.CoolemsClient")


def _build_verifying_ssl_context(cert_path: str) -> ssl_mod.SSLContext | None:
    """Build SSL context that verifies the server certificate."""
    ctx = ssl_mod.create_default_context()
    if cert_path and os.path.exists(cert_path):
        try:
            ctx.load_verify_locations(cafile=cert_path)
            logger.info(f"[TLS] Loading server CA certificate from {cert_path}")
        except Exception as e:
            logger.warning(f"[TLS] Failed to load cert {cert_path}: {e}, using system CAs")
    else:
        logger.debug("[TLS] No custom CA cert configured, using system CAs")
    ctx.check_hostname = False  # IP addresses dont match cert CN/SAN
    ctx.verify_mode = ssl_mod.CERT_REQUIRED
    return ctx


def _build_no_verify_ssl_context() -> ssl_mod.SSLContext:
    """Build SSL context with certificate verification disabled."""
    ctx = ssl_mod.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl_mod.CERT_NONE
    return ctx




    # ---------------------------------------------------------------------------
    # (2026-09-23 v4) FAIL-CLOSED relay TLS builder.
    # The web relay runs on a PUBLIC host - the client->relay link is internet-facing,
    # so an untrusted transport would expose the API key in the first auth frame.
    # build_relay_ssl_context() returns a verifying or pinned context and NEVER falls
    # back to CERT_NONE: when neither option is usable it returns None and the caller
    # refuses to connect (with an actionable log message) instead of degrading.
    # ---------------------------------------------------------------------------

    def _build_relay_ssl_context(verify_certs, pin_fingerprint):
        """SSL context for RELAY-mode connections (internet-facing), fail-closed.

        Returns:
            ssl.SSLContext when either full CA verification or a usable cert pin is
            configured; None otherwise - the caller MUST refuse to connect in that case.
        """
        if verify_certs:
            from config import COOLEMS_SERVER_CERT_PATH  # lazy: ssl_helper must stay import-light
            return _build_verifying_ssl_context(COOLEMS_SERVER_CERT_PATH)
        if pin_fingerprint:
            ctx = _build_pinned_ssl_context(pin_fingerprint)
            if ctx is not None:
                return ctx
        return None


# ---------------------------------------------------------------------------
# (2026-09-01 S3) Certificate pinning -- shared implementation lives in the
# byte-identical sibling module ssl_pinning.py (same file on SERVER and CLIENT,
# parity enforced by tests). Re-exported here so existing import sites keep working.
# ---------------------------------------------------------------------------

from .ssl_pinning import build_pinned_ssl_context  # noqa: E402,F401


def _build_pinned_ssl_context(pin):
    """Build a pinning SSL context from WEB_RELAY_CERT_PIN_FINGERPRINT (or None if unusable)."""
    return build_pinned_ssl_context(pin)
