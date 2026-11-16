"""Strata backend provider package.

Set config.SERVER_BACKEND = "strata" and the COOLEMS SERVER talks to Strata's local
inference endpoint instead of a llama-server process it spawns itself. See provider.py
for what changes and what is reused, and config/strata_servers.json for the endpoints.
"""

from .provider import StrataProvider

__all__ = ["StrataProvider"]
