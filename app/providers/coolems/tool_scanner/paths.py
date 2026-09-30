"""SERVER-root path helper.

The old module lived at app/providers/coolems/tool_scanner.py and used
Path(__file__).resolve().parent x4; this package is one level deeper."""

import pathlib

def _server_root():
    """Resolve the local_ai SERVER root (the directory containing tools/ and config/).

    This package lives one level deeper than the old single-file tool_scanner.py,
    so it takes five parents instead of four:
    tool_scanner/ -> coolems/ -> providers/ -> app/ -> local_ai
    """
    return pathlib.Path(__file__).resolve().parent.parent.parent.parent.parent  # -> local_ai root
