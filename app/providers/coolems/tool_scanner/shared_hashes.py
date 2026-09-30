"""Integrity hashes for every shared/framework module shipped to the CLIENT."""

from .framework_files import get_framework_sources
from .sha_prefix import _sha_prefix
from .shared_sources import get_all_shared_sources

def get_shared_hashes(tools_dir) -> dict:
    """SHA-256 prefixes for every shared module source shipped to the CLIENT.

    Keys mirror get_all_shared_sources()/get_framework_sources() keys so the client can
    verify each delivered module before exec'ing it (fail-closed when hashes are present).
    """
    sources = dict(get_all_shared_sources(tools_dir))
    sources.update(get_framework_sources(tools_dir))
    return {k: _sha_prefix(v) for k, v in sources.items() if v}
