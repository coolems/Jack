"""Command line argument parser for the COOLEMS CLIENT.

Parses port and provider arguments from sys.argv. Tasks run only through the web UI; the
parser starts the server, it does not execute a prompt.
"""

import sys

from client_config import CLIENT_UI_PORT  # single source of truth for the default UI port (CLIENT/client_config/config.py)


def parse_args():
    """Parse command line arguments.

    Returns:
        tuple: (port, provider_arg)
            - port (int): Server port (default from client_config.CLIENT_UI_PORT)
            - provider_arg (str or None): Provider override from CLI
    """
    port = CLIENT_UI_PORT
    provider_arg = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif arg.startswith("port="):
            try:
                port = int(arg.split("=", 1)[1])
            except ValueError:
                pass
            i += 1
        elif arg.startswith("provider="):
            provider_arg = arg.split("=", 1)[1].strip().lower()
            i += 1
        else:
            i += 1

    return port, provider_arg
