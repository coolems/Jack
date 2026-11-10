"""MCP client — one connection per enabled server (2026-12).

Lifecycle: start transport -> `initialize` (protocolVersion + capabilities + clientInfo)
-> `notifications/initialized` -> `tools/list` (cached) -> optional `resources/list` +
`resources/templates/list`. Nothing is fetched until the model asks, and a server that
never connected contributes no definitions.

Result policy (the part that protects the context window):
  * text parts are clipped and the FULL payload spills to a file, so an oversized MCP
    result is a bounded preview + a locator, never a silent truncation;
  * a `blob` (base64) never enters history — it is saved as a file and reported as
    uri + mime + byte size;
  * a server-level tool failure is reported as isError text, so the child/parent loop
    keeps running instead of raising.

A transport that dies mid-request is reconnected but the call is NOT retried: we cannot
know whether the server executed it, and re-running a side-effecting tool would be worse
than telling the model the call did not happen.
"""

import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import McpServerConfig
from .protocol import JsonRpcError, make_transport

logger = logging.getLogger("COOLEMS.MCP.Client")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "coolems-client"
# Bounds come from config.py (MCP_TEXT_PREVIEW_CHARS / MCP_INSTRUCTIONS_MAX_CHARS).
try:
    from config import MCP_TEXT_PREVIEW_CHARS as TEXT_PREVIEW_CHARS, \
        MCP_INSTRUCTIONS_MAX_CHARS as INSTRUCTIONS_MAX_CHARS
except Exception:  # pragma: no cover - defensive
    TEXT_PREVIEW_CHARS = 4000
    INSTRUCTIONS_MAX_CHARS = 1200


@dataclass
class McpToolDef:
    """One tool offered by a server, already namespaced for the model."""

    name: str            # mcp__<server>__<tool>
    server: str
    tool: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)


@dataclass
class McpToolResult:
    """What tools/call gave back, already reduced to model-safe text."""

    text: str
    is_error: bool = False
    spill_path: str = ""


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:60] or "mcp"


def _spill(spill_dir: str, server: str, label: str, content: str) -> str:
    """Write the complete payload outside the repo root (scratch policy: .temp/)."""
    if not spill_dir:
        return ""
    try:
        os.makedirs(spill_dir, exist_ok=True)
        path = os.path.join(spill_dir, f"{_slug(server)}-{_slug(label)}-{uuid.uuid4().hex[:8]}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
    except OSError as e:
        logger.debug("[MCP:%s] spill failed: %s", server, e)
        return ""


def _preview(text: str, spill_path: str) -> str:
    if len(text) <= TEXT_PREVIEW_CHARS:
        return text
    half = TEXT_PREVIEW_CHARS // 2
    note = f"\n...[{len(text) - TEXT_PREVIEW_CHARS} chars omitted"
    note += f" - full payload: {spill_path}]" if spill_path else " - call again with narrower arguments]"
    return text[:half] + note + "\n" + text[-half:]


class McpClient:
    """One server connection. Never raises from connect()/call() - state is reported."""

    def __init__(self, cfg: McpServerConfig, *, spill_dir: str = ""):
        self.cfg = cfg
        self.spill_dir = spill_dir
        self.connected = False
        self.error = ""
        self.instructions = ""
        self.server_info = ""
        self.tools: List[McpToolDef] = []
        self.resources: List[Dict[str, Any]] = []
        self.templates: List[Dict[str, Any]] = []
        self._transport = None

    @property
    def transport(self):
        return self._transport

    async def connect(self) -> bool:
        """Handshake + tool listing. Returns False with a readable `error` on failure."""
        self.connected = False
        self.error = ""
        try:
            self._transport = make_transport(self.cfg, self.cfg.name)
            if hasattr(self._transport, "start"):
                # stdio transports need a process; http transports are ready on request.
                await self._transport.start()
            info = await self._transport.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "clientInfo": {"name": CLIENT_NAME, "version": "1.0"},
            }, self.cfg.timeout_sec)
            await self._transport.notify("notifications/initialized", {})
            self.server_info = str((info or {}).get("serverInfo", {}).get("name") or self.cfg.name)
            raw_instr = (info or {}).get("instructions")
            if isinstance(raw_instr, str) and raw_instr.strip():
                self.instructions = raw_instr.strip()[:INSTRUCTIONS_MAX_CHARS]

            listed = await self._transport.request("tools/list", {}, self.cfg.timeout_sec)
            self.tools = self._parse_tools(listed)

            # Resources are optional capability-wise: a server without them answers with
            # an error object, which must not make the whole connection fail.
            self.resources = await self._soft_list("resources/list")
            self.templates = await self._soft_list("resources/templates/list")
            self.connected = True
            logger.info("[MCP:%s] connected (%s tool(s), %s resource(s))",
                        self.cfg.name, len(self.tools), len(self.resources))
            return True
        except JsonRpcError as e:
            self.error = str(e)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        logger.warning("[MCP:%s] connect failed: %s", self.cfg.name, self.error)
        await self.close()
        return False

    async def _soft_list(self, method: str) -> List[Dict[str, Any]]:
        """Optional capability: a server without resources answers with an error object,
        which must not make the whole connection fail."""
        try:
            out = await self._transport.request(method, {}, self.cfg.timeout_sec)
        except JsonRpcError:
            return []
        raw = (out or {}).get("resources") or (out or {}).get("resourceTemplates") or []
        return [r for r in raw if isinstance(r, dict)]

    def _parse_tools(self, listed: Any) -> List[McpToolDef]:
        raw = (listed or {}).get("tools") or []
        out: List[McpToolDef] = []
        for t in raw:
            if not isinstance(t, dict):
                continue
            tool = str(t.get("name") or "").strip()
            if not tool:
                continue
            schema = t.get("inputSchema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            out.append(McpToolDef(
                name=f"mcp__{self.cfg.name}__{tool}", server=self.cfg.name, tool=tool,
                description=str(t.get("description") or ""), input_schema=schema))
        return out

    async def call_tool(self, tool: str, arguments: Dict[str, Any]) -> McpToolResult:
        if not self.connected or self._transport is None:
            return McpToolResult(f"MCP server '{self.cfg.name}' is not connected"
                                 f" ({self.error or 'never started'}).", is_error=True)
        try:
            result = await self._transport.request("tools/call",
                                                   {"name": tool, "arguments": arguments or {}},
                                                   self.cfg.timeout_sec)
        except JsonRpcError as e:
            message = str(e)
            if "transport closed" in message or "not connected" in message:
                # Reconnect for later calls, but never re-run this one: we cannot know
                # whether the server executed it.
                await self.close()
                await self.connect()
                message += (" - the call may or may not have run; reconnect succeeded, call it "
                            "again only if repeating it is safe.")
            return McpToolResult(message, is_error=True)
        return self._reduce(result, tool)

    def _reduce(self, result: Any, tool: str) -> McpToolResult:
        result = result or {}
        parts = result.get("content") or []
        texts: List[str] = []
        blobs: List[str] = []
        files: List[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                texts.append(str(part.get("text") or ""))
            elif kind == "image" or (kind == "resource" and isinstance(part.get("resource"), dict)
                                     and part["resource"].get("blob")):
                blob = str(part.get("blob") or part.get("resource", {}).get("blob") or "")
                mime = str(part.get("mimeType") or part.get("resource", {}).get("mimeType")
                           or "application/octet-stream")
                saved = self._save_blob(blob, mime, tool)
                if saved:
                    files.append(saved)
                blobs.append(f"[binary payload ({mime}, {len(blob)} base64 bytes)"
                             + (f" saved to {saved} - read/transcribe it if needed]" if saved
                                else " - not shown; it cannot go into the transcript]"))
            elif kind == "resource" and isinstance(part.get("resource"), dict):
                texts.append(str(part["resource"].get("text") or ""))
            else:
                texts.append(f"[unsupported content part: {kind}]")
        body = "\n".join(t for t in texts if t)
        if blobs:
            body = (body + "\n" if body else "") + "\n".join(blobs)
        # Spill ONLY when the payload is oversized; a binary-only result uses the saved
        # binary file as its locator, so we never write a text file describing a blob.
        spill = _spill(self.spill_dir, self.cfg.name, tool, body) if len(body) > TEXT_PREVIEW_CHARS else ""
        if not spill and len(files) == 1:
            spill = files[0]
        text = _preview(body, spill) or "(the server returned no content)"
        return McpToolResult(text, is_error=bool(result.get("isError")), spill_path=spill)

    def _save_blob(self, blob: str, mime: str, label: str) -> str:
        """Decode a base64 payload to a file in the scratch dir. '' when impossible."""
        if not blob or not self.spill_dir:
            return ""
        try:
            import base64
            os.makedirs(self.spill_dir, exist_ok=True)
            path = os.path.join(self.spill_dir,
                                f"{_slug(self.cfg.name)}-{_slug(label)}-{uuid.uuid4().hex[:8]}.bin")
            with open(path, "wb") as f:
                f.write(base64.b64decode(blob))
            return path
        except Exception as e:
            logger.debug("[MCP:%s] blob save failed: %s", self.cfg.name, e)
            return ""

    async def list_resources(self) -> List[Dict[str, Any]]:
        return self.resources

    async def list_templates(self) -> List[Dict[str, Any]]:
        return self.templates

    async def read_resource(self, uri: str) -> McpToolResult:
        if not self.connected or self._transport is None:
            return McpToolResult(f"MCP server '{self.cfg.name}' is not connected"
                                 f" ({self.error or 'never started'}).", is_error=True)
        try:
            out = await self._transport.request("resources/read", {"uri": uri},
                                                self.cfg.timeout_sec)
        except JsonRpcError as e:
            return McpToolResult(str(e), is_error=True)
        contents = (out or {}).get("contents") or []
        texts: List[str] = []
        files: List[str] = []
        for c in contents:
            if not isinstance(c, dict):
                continue
            if c.get("text"):
                texts.append(str(c["text"]))
            elif c.get("blob"):
                blob = str(c["blob"])
                mime = str(c.get("mimeType") or "application/octet-stream")
                saved = self._save_blob(blob, mime, uri)
                if saved:
                    files.append(saved)
                texts.append(f"[binary resource {uri} ({mime}, {len(blob)} base64 bytes)"
                             + (f" saved to {saved}]" if saved else " - not shown]"))
        body = "\n".join(texts) or f"(resource {uri} returned no readable text)"
        spill = _spill(self.spill_dir, self.cfg.name, uri, body) if len(body) > TEXT_PREVIEW_CHARS else ""
        if not spill and len(files) == 1:
            spill = files[0]
        return McpToolResult(_preview(body, spill), is_error=False, spill_path=spill)

    async def close(self) -> None:
        transport = self._transport
        self._transport = None
        self.connected = False
        if transport is not None:
            try:
                await transport.close()
            except Exception as e:
                logger.debug("[MCP:%s] close failed (non-fatal): %s", self.cfg.name, e)


#: Duck-typed check: a stdio transport owns a process and needs start(); the http
#: transport is ready on the first request. Both live in protocol.py.
def is_stdio_transport(transport) -> bool:
    return hasattr(transport, "start") and hasattr(transport, "command")
