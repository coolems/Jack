"""SSL certificate detection for COOLEMS CLIENT.

Checks CLIENT/certs/ and SERVER_ROOT/certs/ for TLS certificates.
Falls back to HTTP if no certs found (standalone distribution mode).
"""

import os


def detect_ssl_config(client_dir, server_root):
    """Detect SSL certificate files for HTTPS support.

    Checks two locations in order:
    1. CLIENT/certs/server.crt + server.key
    2. SERVER_ROOT/certs/server.crt + server.key

    Args:
        client_dir: Path to the CLIENT directory
        server_root: Path to the SERVER root directory

    Returns:
        tuple: (has_ssl, certfile_path, keyfile_path)
            - has_ssl (bool): True if valid certificate pair found
            - certfile_path (str or None): Path to .crt file
            - keyfile_path (str or None): Path to .key file
    """
    # Check CLIENT/certs/ first
    ssl_certfile = os.path.join(client_dir, "certs", "server.crt")
    ssl_keyfile = os.path.join(client_dir, "certs", "server.key")

    if os.path.exists(ssl_certfile) and os.path.exists(ssl_keyfile):
        return True, ssl_certfile, ssl_keyfile

    # Fall back to SERVER_ROOT/certs/
    parent_ssl_certfile = os.path.join(server_root, "certs", "server.crt")
    parent_ssl_keyfile = os.path.join(server_root, "certs", "server.key")

    if os.path.exists(parent_ssl_certfile) and os.path.exists(parent_ssl_keyfile):
        return True, parent_ssl_certfile, parent_ssl_keyfile

    return False, None, None
