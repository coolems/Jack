"""Short-lived tool request WebSocket helpers for CoolemsClientProvider.

Opens a temporary WebSocket connection, authenticates, sends a single request, and closes.
"""

import asyncio
import json
import logging
from typing import Dict, Optional

from config import PROTOCOL_VERSION
from . import _check_protocol, connect_with_failover, NoServerReachable

logger = logging.getLogger("COOLEMS.Provider.CoolemsClient")


async def send_tool_request(provider, req_type: str, recv_timeout: float = 15.0, **kwargs) -> Optional[Dict]:
    """Send a tool-related request to SERVER via short-lived WebSocket.

        recv_timeout: seconds to wait for the single response frame (default 15 s).
        Slow server-side operations (e.g. OCR transcription) pass a larger budget.
        """
    import websockets

    try:
        candidates = provider._get_ws_candidates()
        api_key = provider._load_api_key()

        async with connect_with_failover(
            candidates, max_size=provider.max_msg_size,
            ping_interval=provider.heartbeat_interval, ping_timeout=provider.ping_timeout,
        ) as ws:
            await ws.send(json.dumps({"type": "auth", "api_key": api_key, "role_type": "client",
                                    "protocol_version": PROTOCOL_VERSION}))
            auth_response = json.loads(await asyncio.wait_for(ws.recv(), timeout=provider.handshake_timeout))
            if auth_response.get("type") != "auth_ok":
                logger.error(f"[CLIENT] Tool request auth failed: {auth_response}")
                return None
            _check_protocol(auth_response, f"tool_request:{req_type}")

            payload = {"type": req_type, **kwargs}
            await ws.send(json.dumps(payload))
            raw_msg = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            return json.loads(raw_msg)

    except NoServerReachable as e:
        logger.error(f"[CLIENT] send_tool_request NO SERVER REACHABLE ({req_type}) - {e}")
        return None
    except asyncio.TimeoutError as e:
        logger.error(f"[CLIENT] send_tool_request TIMEOUT ({req_type}): {type(e).__name__} - {e}")
        return None
    except websockets.exceptions.ConnectionClosedError as e:
        logger.error(f"[CLIENT] send_tool_request CONNECTION CLOSED ({req_type}): code={e.code}, reason='{e.reason}'")
        return None
    except websockets.exceptions.ConnectionClosedOK as e:
        logger.warning(f"[CLIENT] send_tool_request connection closed OK ({req_type})")
        return None
    except OSError as e:
        logger.error(f"[CLIENT] send_tool_request OS ERROR ({req_type}): {type(e).__name__} - errno={e.errno} - {e}")
        return None
    except Exception as e:
        exc_name = type(e).__name__
        exc_msg = str(e) if str(e) else repr(e)
        logger.error(f"[CLIENT] send_tool_request FAILED ({req_type}): {exc_name} - {exc_msg}")
        return None
