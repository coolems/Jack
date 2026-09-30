"""DNA JSON data files (agent STATE, not code) shipped with tools_response."""

import os
from .shared_sources import _read_text

def get_dna_data_files(tools_dir):
    """Read DNA JSON data files (agent STATE, not code).

    The SERVER is the source of truth for agent identity/intelligence/lessons.
    These files are sent to each client with tools_response; the CLIENT syncs them
    into its own app/dna directory (its proper location -- NEVER working_root) so the
    delivered DNA modules can read/write them there without any on-disk copies in the
    SERVER distribution.

    Returns:
        Dict mapping file name -> JSON string (empty if not found).
    """
    data = {}
    dna_dir = os.path.join(tools_dir, "dna")
    for fname in ('identity.json', 'intelligence.json', 'lessons.json'):
        path = os.path.join(dna_dir, fname)
        if os.path.exists(path):
            data[fname] = _read_text(path)
    return data
