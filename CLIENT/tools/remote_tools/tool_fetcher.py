"""Tool fetcher — sends/receives tool code over WebSocket from SERVER."""

import asyncio
import json
import logging
from typing import Dict, Optional, List

logger = logging.getLogger("COOLEMS.Tools.Remote.Fetcher")


class ToolFetcher:
    """Sends tool requests to SERVER via the client provider's WebSocket helpers.

    All communication goes through CoolemsClientProvider helper methods
    so it works in both direct and relay modes transparently.
    """

    # Startup fetch retries: the CLIENT often starts before (or while) the
    # SERVER is still booting, which surfaces as a refused connection
    # ([WinError 1225]) or an empty response. Retry with backoff instead of
    # failing on the first attempt.
    FETCH_TOOLS_MAX_ATTEMPTS = 5
    FETCH_TOOLS_RETRY_DELAY_SEC = 2.0

    def __init__(self, provider):
        self._provider = provider  # CoolemsClientProvider instance

    async def fetch_tools(self) -> Optional[Dict]:
        """Request all allowed tool definitions + shared utils from SERVER.

        Retries on transient failures (SERVER not up yet / connection refused /
        no response) with a short backoff before giving up.

        Returns:
            Dict with 'definitions', 'allowed_tools', 'shared_utils_source' or None on error.
        """
        last_error = "no response"
        for attempt in range(1, self.FETCH_TOOLS_MAX_ATTEMPTS + 1):
            try:
                result = await self._provider.send_tool_request("tools_request")
                if result and result.get("type") == "tools_response":
                    logger.info(f"Fetcher: received {len(result.get('definitions', []))} tool definitions (attempt {attempt})")
                    return result
                last_error = f"unexpected tools_response format: {result!r}"
                logger.error(f"Fetcher: unexpected tools_response format: {result}")
            except Exception as e:
                last_error = str(e) or type(e).__name__
                logger.error(f"Fetcher: failed to fetch tools (attempt {attempt}): {e}")

            if attempt < self.FETCH_TOOLS_MAX_ATTEMPTS:
                delay = self.FETCH_TOOLS_RETRY_DELAY_SEC * attempt
                target = getattr(self._provider, "api_url", "") or "SERVER"
                logger.warning(
                    f"Fetcher: SERVER did not deliver tools (attempt {attempt}/{self.FETCH_TOOLS_MAX_ATTEMPTS}) - "
                    f"retrying in {delay:.0f}s. Is the COOLEMS SERVER running and reachable at {target}?"
                )
                await asyncio.sleep(delay)

        logger.error(
            f"Fetcher: giving up after {self.FETCH_TOOLS_MAX_ATTEMPTS} attempts - last error: {last_error}. "
            f"Start the COOLEMS SERVER (code.py in the server root) and make sure its address/port match "
            f"CLIENT config settings.json, then restart the CLIENT."
        )
        return None

    async def fetch_tool_code(self, tool_name: str) -> Optional[Dict]:
        """Request source code for a single tool from SERVER.

        Returns:
            Dict with 'source_code' and 'dependencies', or error dict on failure.
        """
        try:
            result = await self._provider.send_tool_request(
                "tool_code_request", name=tool_name
            )
            if not result:
                return None

            if result.get("error"):
                logger.warning(f"Fetcher: SERVER denied tool '{tool_name}': {result['error']}")
                return {"error": result["error"]}

            if result.get("source_code"):
                logger.info(f"Fetcher: received source for '{tool_name}' ({len(result['source_code'])} chars)")
                return result

            logger.error(f"Fetcher: unexpected tool_code_response: {result}")
            return None

        except Exception as e:
            logger.error(f"Fetcher: failed to fetch code for '{tool_name}': {e}")
            return None
