"""DeepSeek backend provider package.

Set config.SERVER_BACKEND = "deepseek" and the COOLEMS SERVER talks to DeepSeek's hosted
OpenAI-compatible API instead of a local engine. See provider.py for the request/response
transform and models.py for what /models reports, and config/deepseek_servers.json for the
endpoint + Bearer key (gitignored).
"""

from .provider import DeepSeekProvider

__all__ = ["DeepSeekProvider"]
