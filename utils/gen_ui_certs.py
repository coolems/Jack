#!/usr/bin/env python3
"""Generate the CLIENT UI TLS certificates (one-time, local-only).

Creates in <certs_dir>:
  ca.crt      - self-signed "COOLEMS Local CA" root. Import this into your browser ONCE
                so it trusts https://127.0.0.1:8000/ automatically afterwards.
  server.crt  - leaf certificate for the CLIENT UI (https://127.0.0.1:8000/), signed by the CA.
  server.key  - its private key. NEVER commit it (.gitignore *.key keeps it out).

Why this exists: a fresh GitHub clone has no certificates anywhere, so the CLIENT UI starts
HTTP-only (see entry/ssl_config.py) and a browser that previously trusted the old machine's
cert will refuse to connect ("will not connect at all"). Generating a local CA + leaf pair
restores HTTPS with a certificate the user trusts in one import step.

Requires: the 'cryptography' package (init installs it automatically if missing).
Usage:    python utils/gen_ui_certs.py <certs_dir>
Exit codes: 0 = ok, 2 = bad args, 3 = cryptography not installed (stdout says MISSING_DEP)
"""
import datetime
import os
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: gen_ui_certs.py <certs_dir>")
        return 2
    certs_dir = os.path.abspath(sys.argv[1])
    os.makedirs(certs_dir, exist_ok=True)

    try:
        import ipaddress
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption, PrivateFormat)
    except ImportError:
        print("MISSING_DEP")
        return 3

    now = datetime.datetime.now(datetime.timezone.utc)
    ten_years = datetime.timedelta(days=3650)

    def make_key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # ---- CA: self-signed root the browser trusts once (10-year validity) ----
    ca_key = make_key()
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Local"),
        x509.NameAttribute(NameOID.COMMON_NAME, "COOLEMS Local CA"),
    ])
    ca_cert = (x509.CertificateBuilder()
               .subject_name(ca_name)
               .issuer_name(ca_name)
               .public_key(ca_key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=5))
               .not_valid_after(now + ten_years)
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .sign(ca_key, hashes.SHA256()))

    # ---- leaf: the actual UI server certificate (localhost + 127.0.0.1 SANs) ----
    leaf_key = make_key()
    leaf_name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Local"),
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ])
    leaf_cert = (x509.CertificateBuilder()
                 .subject_name(leaf_name)
                 .issuer_name(ca_name)
                 .public_key(leaf_key.public_key())
                 .serial_number(x509.random_serial_number())
                 .not_valid_before(now - datetime.timedelta(minutes=5))
                 .not_valid_after(now + ten_years)
                 .add_extension(san, critical=False)
                 .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                 .sign(ca_key, hashes.SHA256()))

    def write_pem(name: str, data: bytes) -> None:
        with open(os.path.join(certs_dir, name), "wb") as f:
            f.write(data)

    write_pem("ca.crt", ca_cert.public_bytes(Encoding.PEM))
    write_pem("server.crt", leaf_cert.public_bytes(Encoding.PEM))
    write_pem("server.key", leaf_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()))
    print(f"OK {certs_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
