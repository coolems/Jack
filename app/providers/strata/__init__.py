"""Strata backend provider package (2026-11).

Set ``config.SERVER_BACKEND = "strata"`` and the COOLEMS SERVER talks to Strata's local
inference endpoint instead of a llama-server process it spawns itself. See
``provider.py`` for what changes and what is reused, and ``config/strata_servers.json``
for the endpoints we attach to.
"""

from .provider import StrataProvider

__all__ = ["StrataProvider"]
