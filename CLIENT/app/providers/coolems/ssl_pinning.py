"""Certificate pinning for TLS client links (SHA-256 fingerprint of peer cert).

(2026-09-01 S3) Shared between SERVER and CLIENT -- the two copies MUST stay
byte-identical (same pattern as provider_manager.py; see tests/test_protocol_sync_20260820.py
for that parity convention).

TLS still provides encryption in transit, but trust is decided by an EXACT SHA-256
fingerprint of the peer certificate instead of a CA chain. This is the right tool for a
self-signed relay cert: no CA infrastructure needed, yet a MITM presenting ANY other
certificate (even one signed by a trusted CA) fails before application data -- e.g. an
API key -- flows.

Enforcement points (both verified live against websockets 12 / Python 3.12):
  - wrap_socket(): blocking sockets (selector loops, direct use).
  - wrap_bio(): the asyncio proactor transport calls context.wrap_bio() and then drives
    the returned SSLObject via do_handshake()/read()/write(). We return a thin proxy that
    re-checks the fingerprint after each of those calls until it has seen the peer cert
    (checked-once afterwards: the certificate cannot change mid-connection).

Why not ssl.SSLContext.verify_callback? It is not reliably honored across CPython /
OpenSSL builds (verified absent as a real hook in 3.12.8 on this machine), so the pin is
enforced deterministically at the transport edges instead.

Accepted pin formats (see _normalize_pin): "sha256/<base64-of-DER>" or a path to the
peer's .pem/.crt file (fingerprinted at context-build time).
"""

import base64
import hashlib
import logging
import os
import ssl as ssl_mod

logger = logging.getLogger("COOLEMS.TLS.Pinning")


def _normalize_pin(pin):
    """Normalize a configured pin to 'sha256/<base64-of-DER>' or return None."""
    if not pin:
        return None
    pin = str(pin).strip()
    if not pin:
        return None
    if pin.lower().startswith("sha256/"):
        b64 = pin.split("/", 1)[1].strip()
        try:
            decoded = base64.b64decode(b64)
        except Exception:
            logger.warning("[TLS-PIN] Invalid sha256 pin (bad base64): %r", pin[:20])
            return None
        if len(decoded) != 32:
            logger.warning("[TLS-PIN] Invalid sha256 pin (expected 32 DER-hash bytes, got %d)", len(decoded))
            return None
        return "sha256/" + b64
    if os.path.exists(pin):
        try:
            from cryptography import x509 as _x509
            from cryptography.hazmat.primitives.serialization import Encoding
            with open(pin, "rb") as f:
                data = f.read()
            cert_obj = _x509.load_pem_x509_certificate(data)
            der = cert_obj.public_bytes(Encoding.DER)
            return "sha256/" + base64.b64encode(hashlib.sha256(der).digest()).decode()
        except Exception as e:
            logger.warning("[TLS-PIN] Could not fingerprint pin file %s: %s", pin, e)
            return None
    logger.warning("[TLS-PIN] Unrecognized cert pin format (want 'sha256/<b64>' or a .pem path): %r", pin[:40])
    return None


def _peer_cert_fingerprint(ssl_obj):
    """SHA-256 fingerprint of the peer certificate's DER encoding, or None if not available yet."""
    try:
        der = ssl_obj.getpeercert(binary_form=True)
    except (OSError, ValueError):
        return None  # handshake not complete yet (non-blocking bio path) or no cert presented
    if not der:
        return None
    return "sha256/" + base64.b64encode(hashlib.sha256(der).digest()).decode()


class _PinnedSSLObject:
    """Proxy around an SSLObject that enforces the pin after every handshake step.

    asyncio's sslproto drives the object returned by context.wrap_bio() via
    do_handshake()/read()/write()/unwrap(). The peer certificate only becomes readable
    partway through the (possibly multi-step, non-blocking) handshake, so we re-check on
    each call until the fingerprint matches once -- then stop checking (the cert cannot
    change mid-connection). Every other attribute/method delegates to the wrapped object.
    """

    def __init__(self, obj):
        self._obj = obj
        self._pin_checked = False

    def _check(self):
        if self._pin_checked:
            return
        fp = _peer_cert_fingerprint(self._obj)
        if fp is None:
            return  # peer cert not available yet -- re-checked on the next call
        if fp != self._pin:
            raise ssl_mod.SSLError(
                f"Certificate pin mismatch: got {fp[:24]}..., expected {self._pin[:24]}... "
                "(possible MITM or the peer certificate changed - update WEB_RELAY_CERT_PIN_FINGERPRINT)"
            )
        self._pin_checked = True

    def do_handshake(self):
        result = self._obj.do_handshake()
        self._check()
        return result

    def read(self, *args, **kwargs):
        result = self._obj.read(*args, **kwargs)
        self._check()
        return result

    def write(self, *args, **kwargs):
        result = self._obj.write(*args, **kwargs)
        self._check()
        return result

    def unwrap(self):
        result = self._obj.unwrap()
        self._check()
        return result

    def __getattr__(self, name):
        # Delegate everything else (ciphers, add_wakeup, pending, ...) to the real object.
        return getattr(object.__getattribute__(self, "_obj"), name)


class PinnedSSLContext(ssl_mod.SSLContext):
    """SSLContext that accepts the peer certificate only when its SHA-256 fingerprint matches."""

    def __new__(cls, pin_fingerprint):
        # ssl.SSLContext is constructed via __new__(protocol) -- pass the protocol there;
        # keep the pin on the instance for enforcement.
        obj = super().__new__(cls, ssl_mod.PROTOCOL_TLS_CLIENT)
        obj._pin = pin_fingerprint
        return obj

    def __init__(self, pin_fingerprint):
        # PROTOCOL_TLS_CLIENT defaults (CERT_REQUIRED + hostname check) are overridden:
        # the pin IS the trust decision, so chain/hostname checks must not mask a mismatch.
        self.check_hostname = False
        self.verify_mode = ssl_mod.CERT_NONE

    def wrap_socket(self, sock, *args, **kwargs):
        s = super().wrap_socket(sock, *args, **kwargs)
        fp = _peer_cert_fingerprint(s)
        if fp is not None and fp != self._pin:
            raise ssl_mod.SSLError(
                f"Certificate pin mismatch: got {fp[:24]}..., expected {self._pin[:24]}... "
                "(possible MITM or the peer certificate changed - update WEB_RELAY_CERT_PIN_FINGERPRINT)"
            )
        return s

    def wrap_bio(self, incoming=None, outgoing=None, *, server_side=False,
                  server_hostname=None, session=None):
        # CPython 3.12 proactor transport calls context.wrap_bio(incoming=..., outgoing=...)
        # and some builds/paths pass server_side/server_hostname/session as keywords -- accept
        # and forward them so the pinned context stays drop-in compatible with plain ssl.SSLContext.
        obj = super().wrap_bio(incoming=incoming, outgoing=outgoing,
                               server_side=server_side, server_hostname=server_hostname,
                               session=session)
        proxy = _PinnedSSLObject(obj)
        proxy._pin = self._pin  # used by the proxy's _check()
        return proxy


def build_pinned_ssl_context(pin):
    """Build a PinnedSSLContext from a configured pin. Returns None when unusable."""
    normalized = _normalize_pin(pin)
    if not normalized:
        return None
    logger.info("[TLS-PIN] Certificate PINNING enabled (sha256:%s...)", normalized[8:16])
    return PinnedSSLContext(normalized)
