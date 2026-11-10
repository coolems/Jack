"""MCP server config — <CLIENT>/config/mcp_servers.json (2026-12).

Fail-closed by design: a missing or corrupt file means NO servers, and an entry with a
bad shape is dropped (reported) rather than half-applied. Nothing is enabled by default —
an MCP server is an external process or remote endpoint, so enabling it is a human edit in
this file, not something the AI or the code can decide.

Roles are explicit here because the CLIENT, not the SERVER, owns these tool names: a role
that is not listed gets NOTHING (no definition, no dispatch).
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger("COOLEMS.MCP.Config")

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_DIR = os.path.dirname(_CONFIG_DIR)
SERVERS_FILE = os.path.join(_CONFIG_DIR, "mcp_servers.json")

#: Namespace for a tool name: mcp__<name>__<tool>. Anything looser would break the name
#: the model emits back to us (and the SERVER's own name rules).
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")

TRANSPORTS = ("stdio", "http")

# Values come from config.py (single source of truth); these are fallbacks so the module
# still loads in a bare test context. config.py's MCP_* constants win when importable.
try:
    from config import (MCP_TIMEOUT_DEFAULT_SEC as TIMEOUT_DEFAULT_SEC,
                        MCP_TIMEOUT_MAX_SEC as TIMEOUT_MAX_SEC)
except Exception:  # pragma: no cover - defensive
    TIMEOUT_DEFAULT_SEC = 30
    TIMEOUT_MAX_SEC = 300
TIMEOUT_MIN_SEC = 1


@dataclass
class McpServerConfig:
    """One configured MCP server. `enabled` is the ONLY thing that makes it live."""

    name: str
    transport: str
    enabled: bool = False
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    roles: frozenset = field(default=frozenset({"*"}))
    timeout_sec: float = TIMEOUT_DEFAULT_SEC

    def allows_role(self, role) -> bool:
        """'*' = any authenticated role; an unknown role is refused (fail-closed)."""
        if "*" in self.roles:
            return True
        return bool(role) and role in self.roles

    def consent_kind(self) -> str:
        """Which consent card gates this server's first use."""
        return "subprocess" if self.transport == "stdio" else "url"


def _entry(raw: Dict[str, Any], problems: List[str]) -> McpServerConfig | None:
    """Validate ONE entry. Returns None (with a recorded reason) on any bad shape."""
    name = str(raw.get("name") or "").strip()
    if not _NAME_RE.match(name):
        problems.append(f"entry #{len(problems) + 1}: invalid name {name!r}")
        return None

    transport = str(raw.get("transport") or "").strip().lower()
    if transport not in TRANSPORTS:
        problems.append(f"{name}: transport {transport!r} not in {TRANSPORTS}")
        return None

    command = str(raw.get("command") or "").strip()
    url = str(raw.get("url") or "").strip()
    if transport == "stdio" and not command:
        problems.append(f"{name}: stdio needs a command")
        return None
    if transport == "http" and not url:
        problems.append(f"{name}: http needs a url")
        return None

    args = raw.get("args") or []
    if not isinstance(args, list):
        problems.append(f"{name}: args must be a list")
        return None
    args = [str(a) for a in args]

    env = raw.get("env") or {}
    if not isinstance(env, dict):
        problems.append(f"{name}: env must be an object")
        return None
    env = {str(k): str(v) for k, v in env.items()}

    roles = raw.get("roles")
    if roles is None:
        role_set = frozenset({"*"})
    elif isinstance(roles, list):
        role_set = frozenset(str(r).strip() for r in roles)
    else:
        problems.append(f"{name}: roles must be a list")
        return None

    try:
        timeout = float(raw.get("timeout_sec") or TIMEOUT_DEFAULT_SEC)
    except (TypeError, ValueError):
        timeout = TIMEOUT_DEFAULT_SEC
    timeout = min(max(timeout, TIMEOUT_MIN_SEC), TIMEOUT_MAX_SEC)

    return McpServerConfig(
        name=name, transport=transport, enabled=bool(raw.get("enabled")),
        command=command, args=args, env=env, cwd=str(raw.get("cwd") or ""),
        url=url, roles=role_set, timeout_sec=timeout,
    )


def load_servers(path: str | None = None) -> tuple[List[McpServerConfig], List[str]]:
    """Read the config file. Returns (servers, problems) — never raises.

    A missing file is NOT a problem: it means no servers, which is the default state.
    A corrupt file IS reported, because silently ignoring a user's enabled server would
    look like the server was broken.
    """
    target = path or SERVERS_FILE
    problems: List[str] = []
    if not os.path.exists(target):
        return [], []
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        problems.append(f"{os.path.basename(target)} is unreadable ({e}) - no MCP servers")
        logger.warning("[MCP] config unreadable: %s", e)
        return [], problems

    raw_servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(raw_servers, list):
        problems.append(f"{os.path.basename(target)} has no 'servers' list - no MCP servers")
        return [], problems

    out: List[McpServerConfig] = []
    for raw in raw_servers:
        if not isinstance(raw, dict):
            problems.append(f"entry #{len(out) + 1}: not an object")
            continue
        cfg = _entry(raw, problems)
        if cfg is not None:
            out.append(cfg)
    return out, problems


def load_servers_safe(path: str | None = None) -> List[McpServerConfig]:
    """Enabled servers only. Callers that must never fail (startup, schema build)."""
    servers, problems = load_servers(path)
    if problems:
        logger.warning("[MCP] %d config problem(s): %s", len(problems), "; ".join(problems[:5]))
    return [s for s in servers if s.enabled]
