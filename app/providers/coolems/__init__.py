"""
    COOLEMS distributed provider protocol (SERVER side).

    TRANSPORT REALITY (2026-08-20 doc fix): the wire protocol is WebSocket TEXT frames
    carrying JSON objects -- there is NO binary length-prefix framing anywhere in this
    codebase. The old send_frame()/recv_frame() helpers described below were dead code
    (never called) and have been removed along with the misleading docstring.

    Message types (JSON, one object per WS text frame):

        Handshake:
            auth          -> {type:"auth", api_key, role_type, protocol_version}
            auth_ok       <- {type:"auth_ok", protocol_version, role, email, allowed_models,
                              allowed_tools, context_window}
            error         <- {type:"error", message}

        Chat (long-lived connection; one in-flight request per connection):
            request       -> {type:"request", model, messages, temperature, enable_thinking,
                              conv_id, tools}
            content       <- {type:"content", content}
            thinking      <- {type:"thinking", content}
            tool_calls    <- {type:"tool_calls", ...}
            token_stats   <- {type:"token_stats", prompt_tokens, generated_tokens, ...}
            response      <- {type:"response", content}
            done          <- {type:"done"}
            stop          -> {type:"stop", conv_id}

        Control (short round-trips; see CLIENT control_channel for the persistent variant):
            health_check  -> {}            health_ok     <- {models:[...]} | error
            models_request-> {}            models_response <- {models:[{name,size}]}
            current_model -> {}            current_model_response <- {model}
            model_switch  -> {model}       model_switch_response <- {success, message}
            tools_request -> {}            tools_response <- {definitions, allowed_tools,
                                    shared_sources, config_constants, python_exec_blocked_libs,
                                    dna_data, system_prompt, shared_hashes, tool_hashes}
            tool_code_request -> {name}    tool_code_response <- {source_code, shared_deps}

    PROTOCOL VERSIONING (2026-08-20): every auth frame carries "protocol_version" and the
    server echoes it back in auth_ok. The authoritative value is config.PROTOCOL_VERSION;
    a mismatch is rejected with an actionable error (legacy clients without the field are
    tolerated for one release cycle with a loud warning).

    Shared helpers below: WebSocket URL/address parsing, API key loading.
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
        """Build WebSocket URL with proper scheme (ws:// or wss://) and IPv6 formatting.

        *path* is an optional route suffix (e.g. "/ws/brain" for the VPS relay router);
        it must start with "/" when given.

        Examples:
            ("150.30.23.1", 8765, False) -> "ws://150.30.23.1:8765"
            ("::1", 8443, True)          -> "wss://[::1]:8443"
            ("relay.example.com", 8443, True, "/ws/brain") -> "wss://relay.example.com:8443/ws/brain"

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


def _get_all_valid_api_keys() -> set:
    """Get a set of ALL active API keys from .api_keys.json.

    Returns empty set if no key file exists or no active keys are found.
    Shared by server_provider for auth validation (avoids double I/O).

    """
    api_keys_path = os.path.join(_resolve_config_dir(), ".api_keys.json")

    if not os.path.exists(api_keys_path):
        return set()

    try:
        with open(api_keys_path, "r", encoding="utf-8") as f:
            keys_data = json.load(f)
        valid = set()
        for key_entry in keys_data:
            if isinstance(key_entry, dict) and key_entry.get("is_active", True):
                valid.add(key_entry["key"])
        return valid
    except (json.JSONDecodeError, KeyError, OSError, TypeError) as e:
        logger.error(f"[COOLEMS] Failed to load API keys from {api_keys_path}: {e}")

    return set()
    