"""Command line argument parser for COOLEMS CLIENT.

Parses port and provider arguments from sys.argv.

NOTE: The old forced-prompt CLI task mode (prompt='...' mode=...) was removed -
tasks are only run through the web UI now. See plan_20260823_1133.md in working_root.
"""

import sys


def parse_args():
    """Parse command line arguments.

    Returns:
        tuple: (port, provider_arg)
            - port (int): Server port (default 8000)
            - provider_arg (str or None): Provider override from CLI
    """
    port = 8000
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
