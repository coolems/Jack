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
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger("COOLEMS.MCP.Config")

# (2026-12 audit fix) This module lives in <CLIENT>/app/mcp/ - the CONFIG directory is
# <CLIENT>/config/, TWO levels up from HERE. The old code went only one level up and
# pointed SERVERS_FILE at <CLIENT>/app/mcp/mcp_servers.json (never created, never read):
# the real user file in <CLIENT>/config/ was silently ignored at boot AND the settings
# save wrote a second orphaned file next to this module. CLIENT_DIR is kept for tests.
_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))          # .../app/mcp
_APP_DIR = os.path.dirname(_CONFIG_DIR)                           # .../app
CLIENT_DIR = os.path.dirname(_APP_DIR)                            # <CLIENT>
SERVERS_FILE = os.path.join(CLIENT_DIR, "config", "mcp_servers.json")

#: Namespace for a tool name: mcp__<name>__<tool>. Anything looser would break the name
#: the model emits back to us (and the SERVER's own name rules). A single '_' is allowed;
#: '__' is rejected separately in _entry(), because split_name() partitions on the FIRST
#: '__' - a server named 'my__srv' would advertise 'mcp__my__srv__echo' and then look up
#: server 'my', so its tools could never be called.
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
    if '__' in name or name.startswith('_') or name.endswith('_'):
        # 'mcp__<name>__<tool>' is split on the FIRST '__' (registry.split_name), so the
        # name must not contain '__' AND must not start or end with '_': 'a_' would
        # advertise 'mcp__a___echo', which splits to server 'a' + tool '_echo' (the wrong
        # server), and '_' advertises 'mcp___echo', which is not even recognised as an MCP
        # name - both contribute tools that can never be dispatched.
        problems.append(f"{name}: name must not contain '__' or start/end with '_' "
                        "(it breaks the mcp__<name>__<tool> namespace)")
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
    if transport == "http":
        # Only http/https endpoints. Anything else (file://, ftp://, a bare path) is a
        # shape the transport cannot honour and the SSRF gate cannot classify.
        if not re.match(r"^https?://", url, re.IGNORECASE):
            problems.append(f"{name}: http url must start with http:// or https://")
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
    # A non-finite value survives the clamp ('nan' is valid JSON, and min(max(nan,1),300)
    # is nan) and then breaks the request: asyncio.wait_for(timeout=nan) times out
    # instantly and int(nan) raises ValueError, so every call would read as a broken
    # server. An unusable timeout means "unset" -> the default.
    if not math.isfinite(timeout):
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
    seen: set = set()
    for raw in raw_servers:
        if not isinstance(raw, dict):
            problems.append(f"entry #{len(out) + 1}: not an object")
            continue
        cfg = _entry(raw, problems)
        if cfg is None:
            continue
        # A duplicate name would silently shadow the earlier entry: McpRegistry keys clients
        # by name, so the first server would never start and its tools would dispatch to the
        # last one. The settings POST rejects duplicates; a hand-edited file gets the same
        # rule reported here instead of a quiet overwrite.
        key = cfg.name.lower()
        if key in seen:
            problems.append(f"{cfg.name}: duplicate name - the later entry is ignored")
            continue
        seen.add(key)
        out.append(cfg)
    return out, problems


def load_servers_safe(path: str | None = None) -> List[McpServerConfig]:
    """Enabled servers only. Callers that must never fail (startup, schema build)."""
    servers, problems = load_servers(path)
    if problems:
        logger.warning("[MCP] %d config problem(s): %s", len(problems), "; ".join(problems[:5]))
    return [s for s in servers if s.enabled]
