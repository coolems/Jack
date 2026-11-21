"""OpenAI backend provider package.

Set config.SERVER_BACKEND = "openai" and the COOLEMS SERVER talks to OpenAI's hosted
OpenAI-compatible API instead of a local engine. See provider.py for the request/response
transform, models.py for what /models reports plus the static per-family metadata, and
config/openai_servers.json for the endpoint + Bearer key (gitignored).
"""

from .provider import OpenAIProvider

__all__ = ["OpenAIProvider"]
