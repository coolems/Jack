"""DNA JSON data files (agent identity) shipped with tools_response."""

import os

from .shared_sources import _read_text

DNA_JSON_FILES = ("identity.json",)


def get_dna_data_files(server_root):
    """Read the DNA JSON files the CLIENT needs in memory (agent identity).

    The SERVER is the source of truth: app/dna/identity.json. The payload rides on
    tools_response so the delivered app.dna.* modules can build the identity without
    any DNA file on the CLIENT disk (the loader installs it via app.dna.set_dna_data).

    Args:
        server_root: The SERVER root folder (the one containing app/ and tools/).

    Returns:
        Dict mapping file name -> JSON string (empty when nothing is found).
    """
    data = {}
    dna_dir = os.path.join(server_root, "app", "dna")
    for fname in DNA_JSON_FILES:
        path = os.path.join(dna_dir, fname)
        if os.path.exists(path):
            data[fname] = _read_text(path)
    return data
