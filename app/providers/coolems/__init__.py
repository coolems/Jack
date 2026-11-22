"""COOLEMS distributed provider protocol (SERVER side).

Wire transport: WebSocket TEXT frames, one JSON object per frame - no binary framing.

  auth        -> {type:"auth", api_key, role_type, protocol_version}
  auth_ok     <- {type:"auth_ok", protocol_version, role, email, allowed_models,
                  allowed_tools, context_window}
  error       <- {type:"error", message}
  request     -> {type:"request", model, messages, temperature, enable_thinking, conv_id, tools}
  content     <- {type:"content", content}
  thinking    <- {type:"thinking", content}
  tool_calls  <- {type:"tool_calls", ...}
  token_stats <- {type:"token_stats", prompt_tokens, generated_tokens, ...}
  response    <- {type:"response", content}
  done        <- {type:"done"}
  stop        -> {type:"stop", conv_id}
  balance_request  -> {type:"balance_request", model, req_id?}
  balance_response <- {type:"balance_response", req_id?, enabled, balance}
                      balance: {currency, total, granted, topped_up, is_available}
                      or null (local model / no key / endpoint failed).
                      enabled=false when the client's profile has
                      show_provider_balance off (the number is never revealed).

request.enable_thinking is a bool (show reasoning or not) or a canonical level
"none"/"low"/"medium"/"high". The SERVER translates a level into the serving model's own
template vocabulary (app/providers/llama/thinking.py) and never echoes a model-specific
name. Both shapes are valid on one protocol version: a bool selects display only, and a
level reaching a backend without thinking support reads as truthy - never a failed request.
"""

import json
import logging
import os

logger = logging.getLogger("COOLEMS.CoolemsProtocol")


# ─── WebSocket Relay Helpers (shared by client & server providers) ──

def _parse_web_relay_address(addr, default_port):
    """Parse host:port from address string, supporting IPv4, IPv6 (bracketed), and domains.

    Examples:
        "150.30.23.1:8765"  -> ("150.30.23.1", 8765)
        "[::1]:8443"        -> ("::1", 8443)
        "coolems.com"       -> ("coolems.com", default_port)

    """
    if addr.startswith("["):
        bracket_end = addr.find("]")
        if bracket_end == -1:
            raise ValueError(f"Invalid IPv6 address (missing closing bracket): {addr}")
        host = addr[1:bracket_end]
        port_str = addr[bracket_end + 1:]
        if port_str.startswith(":"):
            port = int(port_str[1:])
        else:
            port = default_port
    elif ":" in addr:
        host, port = addr.rsplit(":", 1)
        port = int(port)
    else:
        host = addr
        port = default_port

    return host, port


def _build_websocket_url(host, port, use_ssl, path=""):
        """WebSocket URL with the right scheme (ws/wss) and IPv6 bracket formatting.

        *path* is an optional route suffix starting with "/" (e.g. "/ws/brain").
        ("::1", 8443, True) -> "wss://[::1]:8443".
        """
        scheme = "wss" if use_ssl else "ws"
        suffix = ""
        if path:
            p = str(path)
            if not p.startswith("/"):
                p = "/" + p
            suffix = p.rstrip("/") or "/"
        # Format host for URL (IPv6 addresses need brackets in URLs)
        if ":" in str(host):
            return f"{scheme}://[{host}]:{port}{suffix}"
        return f"{scheme}://{host}:{port}{suffix}"

    
def _resolve_config_dir():
    """Resolve the project config directory path relative to this module."""
    _this_dir = os.path.dirname(os.path.abspath(__file__))  # app/providers/coolems/
    _root_dir = os.path.abspath(os.path.join(_this_dir, "..", ".."))  # app/
    _project_root = os.path.dirname(_root_dir)  # project root (resolved from this file's location; e.g. <INSTALL_DIR>/Jack)
    return os.path.join(_project_root, "config")


def _load_coolems_api_key() -> str:
    """Load the first active API key from .api_keys.json in the config directory.

    Returns empty string if no key file exists or no active key is found.
    Shared by both server_provider and client_provider to avoid duplication.

    """
    api_keys_path = os.path.join(_resolve_config_dir(), ".api_keys.json")

    if not os.path.exists(api_keys_path):
        return ""

    try:
        with open(api_keys_path, "r", encoding="utf-8") as f:
            keys_data = json.load(f)
        for key_entry in keys_data:
            if isinstance(key_entry, dict) and key_entry.get("is_active", True):
                return key_entry["key"]
    except (json.JSONDecodeError, KeyError, OSError, TypeError) as e:
        logger.error(f"[COOLEMS] Failed to load API keys from {api_keys_path}: {e}")

    return ""
