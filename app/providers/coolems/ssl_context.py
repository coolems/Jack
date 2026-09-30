"""SSL/TLS context generation for Coolems direct WebSocket server.

Auto-generates self-signed certificate with LAN IP SANs if no certs provided.
"""

import datetime
import ipaddress
import logging
import os
import ssl as ssl_mod

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")


def _create_direct_ws_ssl_context(cert_path="", key_path=""):
    """Create SSL context for direct WebSocket connections. Auto-generates self-signed cert if not provided."""

    ctx = ssl_mod.create_default_context(ssl_mod.Purpose.CLIENT_AUTH)

    if cert_path and key_path:
        logger.info(f"[SERVER] Using user-provided SSL certs: {cert_path}, {key_path}")
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return ctx

    logger.warning("[SERVER] No SSL certificates provided - generating self-signed certificate")

    import cryptography
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Local"),
        x509.NameAttribute(NameOID.COMMON_NAME, "coolems-direct-ws"),
    ])

    # Build SANs: localhost + all LAN IPs for remote client connections
    import socket as _socket_mod

    san_names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]

    try:
        hostname = _socket_mod.gethostname()
        for addr_info in _socket_mod.getaddrinfo(hostname, None):
            ip = addr_info[4][0] if len(addr_info[4]) > 0 else None
            if ip and not ip.startswith("127.") and not ip.startswith("::"):
                try:
                    san_names.append(x509.IPAddress(ipaddress.ip_address(ip)))
                except ValueError:
                    pass

        import psutil as _psutil
        for iface, addrs in _psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == _socket_mod.AF_INET and not addr.address.startswith("127."):
                    try:
                        san_names.append(x509.IPAddress(ipaddress.ip_address(addr.address)))
                    except ValueError:
                        pass
    except ImportError:
        logger.debug("[SERVER] psutil not available - using only localhost for SSL SANs")
    except Exception as _e:
        logger.warning(f"[SERVER] Could not auto-detect LAN IPs for SSL cert: {_e}")

    seen = set()
    unique_sans = []
    for san in san_names:
        key_val = str(san.value)
        if key_val not in seen:
            seen.add(key_val)
            unique_sans.append(san)

    cert_san_list = x509.SubjectAlternativeName(unique_sans)
    logger.info(f"[SERVER] SSL cert SANs: {[str(s.value) for s in unique_sans]}")

    cert = (x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
            .add_extension(cert_san_list, critical=False)
            .sign(key, hashes.SHA256()))

    cert_pem = cert.public_bytes(Encoding.PEM)
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())

    config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "config")
    cert_file = os.path.join(config_dir, "coolems_direct_ws_cert.pem")
    key_file = os.path.join(config_dir, "coolems_direct_ws_key.pem")

    with open(cert_file, "wb") as f:
        f.write(cert_pem)
    with open(key_file, "wb") as f:
        f.write(key_pem)

    # (2026-09-01 S5 fix) the private key is written unencrypted next to real API keys;
    # tighten its mode so only the owner can read it. Best-effort: on Windows this is a
    # no-op (no POSIX perms) and any failure must never block TLS startup.
    for _f in (key_file, cert_file):
        try:
            os.chmod(_f, 0o600)
        except OSError as _e:
            logger.debug(f"[SERVER] Could not chmod {_f} to 0600 (non-POSIX or locked): {_e}")

    ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
    logger.info(f"[SERVER] Self-signed cert saved to {cert_file}")
    return ctx
