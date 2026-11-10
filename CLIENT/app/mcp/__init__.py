"""app.mcp — Model Context Protocol servers exposed as CLIENT-local tools (2026-12).

Modules, one job each:
    config.py    read <CLIENT>/config/mcp_servers.json (nothing enabled by default)
    protocol.py  newline-delimited JSON-RPC 2.0 over stdio pipes or an HTTP POST
    client.py    one connection per server: handshake, tools/list, tools/call, resources
    registry.py  the live set of connections + the tool definitions they contribute

Deliberately NO dependency on the `mcp` SDK: the CLIENT pins httpx/anyio only, and the
protocol we need (initialize, tools/list, tools/call, resources/*) is plain JSON-RPC 2.0.
A self-contained client means an MCP server cannot drag a new dependency tree into the
CLIENT's pinned venv.

Trust model: an MCP server is an EXTERNAL process or remote endpoint. It is started only
when the human enabled it in mcp_servers.json, and its first use still asks a consent card
(stdio -> 'subprocess', http -> 'url'). Server-supplied instructions and tool descriptions
are UNTRUSTED text: they reach the model as data, never as policy.
"""

from .config import McpServerConfig, load_servers, load_servers_safe
from .protocol import JsonRpcError, StdioTransport, StreamableHttpTransport
from .client import McpClient, McpToolResult
from .registry import McpRegistry, get_registry, set_registry, wire_mcp

__all__ = [
    "McpServerConfig", "load_servers", "load_servers_safe",
    "JsonRpcError", "StdioTransport", "StreamableHttpTransport",
    "McpClient", "McpToolResult",
    "McpRegistry", "get_registry", "set_registry", "wire_mcp",
]
