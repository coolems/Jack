"""Relay response adapters and message handlers for the brain relay link.

_RelayTargetAdapter tags every outgoing JSON frame with 'target_client' so the shared
brain socket can route frames to exactly one client; _relay_send() is the one-frame
helper around it. The model_switch / tools_request / tool_code_request handlers all run
their authorization against the RELAYED CLIENT's profile (S1 fix) - never the brain
socket's own admin profile.
"""

import json
import logging

from app.auth_gateway.model_resolver import is_model_allowed

logger = logging.getLogger("COOLEMS.Provider.CoolemsServer")

class _RelayTargetAdapter:
    """WebSocket wrapper that tags EVERY outgoing JSON frame with target_client.

    (2026-09-01 S1 fix / Phase 3) The brain<->relay link is ONE shared socket for N clients;
    the relay only routes a brain frame to a client when it carries a valid 'target_client'.
    llama streaming writes raw frames through this adapter, so every content/thinking/
    tool_calls chunk reaches exactly the requesting client. Non-JSON or already-tagged
    frames pass through unchanged (idempotent).
    """

    def __init__(self, ws, target_client: str):
        self._ws = ws
        self._target = target_client or ""

    async def send(self, text):
        if self._target and isinstance(text, str) and text.lstrip().startswith("{"):
            try:
                frame = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                frame = None
            if isinstance(frame, dict) and "target_client" not in frame:
                frame["target_client"] = self._target
                text = json.dumps(frame)
        await self._ws.send(text)

    def __getattr__(self, name):
        # forward everything else (e.g. remote_address) to the real socket
        return getattr(self._ws, name)


async def _relay_send(ws, msg: dict, target_client: str = ""):
    """Send one brain->client response frame over the relay link with its routing tag."""
    if target_client and "target_client" not in msg:
        msg = {**msg, "target_client": target_client}
    await ws.send(json.dumps(msg))


async def _handle_relay_model_switch(ws, server_provider, model_filename, msg):
    """Handle model_switch message from relay (S1 fix: per-client authorization).

    *msg* carries '_relay_from' so the check runs against the RELAYED CLIENT's profile.
    The brain socket's own user info is NEVER consulted here -- pre-fix it held the
    BRAIN's admin profile, so any client behind the relay could switch to (and restart
    into) ANY model on the box.
    """
    client_id = msg.get("_relay_from") or "unknown"
    user_info = await _resolve_relay_client_user_info(ws, msg, server_provider)
    if not user_info:
        logger.warning(f"[SERVER] Relay model switch DENIED for unknown/unresolvable relayed client {client_id!r} (fail-closed)")
        await _relay_send(ws, {
            "type": "model_switch_response",
            "success": False,
            "message": f"Model switch denied for relayed client {client_id!r} (unresolvable profile).",
        }, client_id)
        return

    local = server_provider._get_local_provider()

    # (2026-08-23 security fix + dedupe): EXACT folder-name matching via the shared
    # auth_gateway resolver - no normalization, no bidirectional substring match.
    all_models = await local.get_models_list()
    known_folders = {
        m["name"].lower(): m["folder"]
        for m in all_models
        if isinstance(m, dict) and m.get("name") and m.get("folder")
    }
    model_permitted = is_model_allowed(user_info, model_filename, known_folders)
    if not model_permitted:
        logger.warning(f"[SERVER] Model switch DENIED via relay: '{model_filename}' is not in client role={user_info.get('role')} allowed models (client={client_id})")
        await _relay_send(ws, {
            "type": "model_switch_response",
            "success": False,
            "message": f"Model '{model_filename}' is not available for your profile.",
        }, client_id)
        return

    try:
        # (2026-09 threadless refactor): direct await - switch_model() is async now and the
        # whole reload wait loop runs on this event loop (no executor thread, no nested loop).
        success, message = await local.switch_model(model_filename)
        await _relay_send(ws, {
            "type": "model_switch_response",
            "success": success,
            "message": message
        }, client_id)
    except Exception as e:
        logger.error(f"[SERVER] Model switch error: {e}", exc_info=True)
        await _relay_send(ws, {
            "type": "model_switch_response",
            "success": False,
            "message": str(e)
        }, client_id)
async def _resolve_relay_client_user_info(ws, msg: dict, server_provider):
    """Resolve the RELAYED CLIENT's user_info for a proxied frame.

    (2026-09-23 v4) KEY-BASED AUTHENTICATION - same logic as direct mode:
      The relay tags every client frame with '_relay_from' = that client's id and, in
      'client_connected', forwards the client's own API key (the one it used to
      authenticate against the relay). We run that key through OUR OWN AuthGateway
      against config/.api_keys.json + profiles.json: role/email/permissions come from
      server-side key lookup - never from a name the relay or the client sends.

    Returns None (fail-closed) when:
      - no _relay_from tag (frame not attributable to a known client), or
      - the client is unknown on this link / its key was never forwarded, or
      - that key has no valid profile in our config (unknown/inactive/no profile).

    Never falls back to the brain socket's own user info attribute -- that holds the
    BRAIN's own (admin) profile, and falling back to it would silently re-grant admin
    privileges to every client behind the relay.
    """
    client_id = msg.get("_relay_from") or ""
    keys: dict = getattr(ws, "_relay_client_keys", None) or {}
    api_key = keys.get(client_id) if client_id else None
    if not api_key:
        logger.warning(
            f"[SERVER] Relay request DENIED - no API key known for relayed client "
            f"{client_id!r} (unknown client or relay did not forward its key). Fail-closed."
        )
        return None

    user_info = server_provider._auth_gateway.authenticate(api_key)
    if not user_info:
        logger.warning(
            f"[SERVER] Relay request DENIED - API key of relayed client {client_id!r} "
            f"has no valid entry in our config/.api_keys.json + profiles.json. Fail-closed."
        )
    return user_info


async def _handle_relay_tools_request(ws, msg: dict, server_provider):
    """Answer a relayed tools_request using the RELAYED CLIENT's permissions (S1 fix)."""
    client_id = msg.get("_relay_from") or "unknown"
    user_info = await _resolve_relay_client_user_info(ws, msg, server_provider)
    if not user_info:
        await _relay_send(ws, {
            "type": "tools_response",
            "error": f"Tools request denied for relayed client {client_id!r} (unresolvable profile).",
        }, client_id)
        return
    from ..ws_client_handler import _handle_tools_request
    # Route the delegated handler's response frame(s) back to THIS client only.
    await _handle_tools_request(_RelayTargetAdapter(ws, client_id), user_info, server_provider)


async def _handle_relay_tool_code_request(ws, msg: dict, server_provider):
    """Answer a relayed tool_code_request using the RELAYED CLIENT's permissions (S1 fix)."""
    client_id = msg.get("_relay_from") or "unknown"
    user_info = await _resolve_relay_client_user_info(ws, msg, server_provider)
    if not user_info:
        await _relay_send(ws, {
            "type": "tool_code_response",
            "error": f"Tool code request denied for relayed client {client_id!r} (unresolvable profile).",
        }, client_id)
        return
    from ..ws_client_handler import _handle_tool_code_request
    # Route the delegated handler's response frame(s) back to THIS client only.
    await _handle_tool_code_request(_RelayTargetAdapter(ws, client_id), msg, user_info, server_provider)

