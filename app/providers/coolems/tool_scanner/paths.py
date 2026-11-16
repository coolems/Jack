"""SERVER-root path helper.

Five parents below the SERVER root - see _server_root().
"""

import pathlib

def _server_root():
    """Resolve the SERVER root (the directory containing tools/ and config/).

    This package sits four levels below it: tool_scanner/ -> coolems/ -> providers/ -> app/.
    """
    return pathlib.Path(__file__).resolve().parent.parent.parent.parent.parent  # -> local_ai root
