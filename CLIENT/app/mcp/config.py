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

#: RFC 7230 field-name token characters. Anything looser (space, ':', CR, LF) would let a
#: config entry forge an extra header line or break the request framing.
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
#: Headers the transport owns: httpx builds Host/Content-Length/Content-Type/Accept itself
#: (the streamable transport sets Accept per request), and Mcp-Session-Id comes back from
#: the server. Accepting an override here would silently ignore what the entry declared.
_HEADER_DENY = frozenset({
    "host", "content-length", "content-type", "accept", "transfer-encoding",
    "connection", "keep-alive", "upgrade", "expect", "te", "trailer",
    "mcp-session-id", "mcp-protocol-version",
})
#: A header value may hold '${NAME}' instead of the secret itself. Only a bare variable
#: name is expandable, so a value can never smuggle an expression. A '${' left over after
#: the valid references are stripped ('Bearer ${LIUM', '${${X}}') is malformed: it would
#: expand to nothing and go out as a literal, so the endpoint answers 401 for what looks
#: like a bad key - rejected at save time instead.
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: A remote endpoint needs a handful of headers at most; the cap keeps one entry from
#: inflating every request of this CLIENT.
MAX_HEADERS_PER_SERVER = 16
#: Combined size of all header names + values, so a config entry cannot grow every request.
MAX_HEADERS_TOTAL_CHARS = 4096
#: A header value must be printable ASCII on a single line. CR/LF would forge an extra
#: request line, NUL and other control bytes are rejected by the HTTP layer anyway - and a
#: non-ASCII byte cannot be encoded as a header at all. Checked again after '${NAME}'
#: expansion, because the environment is where the value actually comes from.
_HEADER_VALUE_BAD = re.compile(r"[^ -~]")

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
    #: Extra request headers for the http transport (e.g. Authorization for a bearer-token
    #: endpoint). Values may reference an environment variable as ${NAME}; the reference is
    #: what lives in the file, the secret stays in the environment (see resolve_headers()).
    headers: Dict[str, str] = field(default_factory=dict)
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

    def resolve_headers(self) -> tuple[Dict[str, str], List[str]]:
        """Request headers with '${NAME}' expanded from the environment.

        Returns (headers, problems): `problems` are reasons to refuse the connection - an
        unset/empty variable, or an expanded value that cannot be sent as a header. Neither
        the value nor the environment content is ever echoed (it may be a credential).
        """
        out: Dict[str, str] = {}
        problems: List[str] = []
        total = 0
        for name, raw in self.headers.items():
            names = _ENV_REF_RE.findall(raw)
            if names:
                unset = [n for n in names if not (os.environ.get(n) or "").strip()]
                if unset:
                    # Reported by NAME so the user sets the right variable; the value itself
                    # is never echoed (it may be a credential).
                    problems.append(f"{name} -> ${{{unset[0]}}} is not set")
                    continue
                value = _ENV_REF_RE.sub(lambda m: os.environ.get(m.group(1), ""), raw)
                # A variable exported from a file usually carries a trailing newline; leading
                # and trailing whitespace is insignificant in a header value (RFC 7230).
                value = value.strip()
            else:
                # Same trim as the '${NAME}' branch: leading/trailing whitespace is
                # insignificant in a header value (RFC 7230) and the HTTP layer refuses it
                # outright - an error there quotes the value, which may be the secret.
                value = raw.strip()
            # Re-checked AFTER expansion: the file was clean, but the environment is where the
            # value really comes from and it is not validated at save time. CR/LF there would
            # forge an extra request line, and httpx fails the request with an error that
            # quotes the whole value - which is the secret.
            if not value or _HEADER_VALUE_BAD.search(value):
                problems.append(f"{name} -> its value is empty or holds a control or "
                                "non-ASCII character and cannot be sent as a header")
                continue
            # The save-time cap counted the '${NAME}' reference; the expanded secret is what
            # actually goes on every request, so the combined size is bounded here too.
            total += len(name) + len(value)
            if total > MAX_HEADERS_TOTAL_CHARS:
                # Reported ONCE, naming the header that crossed the limit: blaming every
                # remaining header would send the user to fix the wrong one.
                problems.append(f"{name} -> the headers together exceed "
                                f"{MAX_HEADERS_TOTAL_CHARS} characters")
                break
            out[name] = value
        return out, problems


def header_mapping(cfg: Any) -> Dict[str, str]:
    """A config entry's headers as a plain dict, whatever the object actually holds.

    Entries built by _entry() always carry a validated dict; a registry constructed from
    hand-made configs can hold anything (None, an int, a non-mapping). status() and the
    settings round-trip must report that as 'no headers' instead of raising - they run on
    the UI request path.
    """
    raw = getattr(cfg, "headers", None)
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items()}


def _parse_headers(raw: Any, name: str, problems: List[str]) -> Dict[str, str] | None:
    """Validate the 'headers' object of an entry. None when the shape is unusable.

    Values keep '${NAME}' as written: expansion happens at connect time
    (McpServerConfig.resolve_headers), so a config save never copies a secret from the
    environment into the file.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append(f"{name}: headers must be an object")
        return None
    out: Dict[str, str] = {}
    seen: set = set()
    total = 0
    for k, v in raw.items():
        key = str(k).strip()
        if not _HEADER_NAME_RE.match(key):
            problems.append(f"{name}: invalid header name {k!r} (letters, digits and "
                            "!#$%&'*+.^_`|~- only, max 64 chars)")
            return None
        if key.lower() in _HEADER_DENY:
            problems.append(f"{name}: header {key!r} is set by the transport itself and "
                            "cannot be overridden here")
            return None
        if not isinstance(v, str):
            # A JSON value that is not a string ('null', 5, true, ["a"]) would be coerced
            # into the literal text 'None' / '5' / "['a']" and sent as the header - an
            # authentication that silently does the wrong thing.
            problems.append(f"{name}: header {key!r} must be a string value")
            return None
        value = v.strip()
        if not value or _HEADER_VALUE_BAD.search(value):
            problems.append(f"{name}: header {key!r} needs a non-empty value of printable "
                            "ASCII on one line")
            return None
        # A leftover '${' after the valid references are stripped means the reference is
        # malformed ('Bearer ${LIUM', '${${X}}'): it would expand to nothing and go out as a
        # literal, so the endpoint answers 401 for what looks like a bad key.
        if "${" in _ENV_REF_RE.sub("", value):
            problems.append(f"{name}: header {key!r} has a malformed '${{NAME}}' reference "
                            "(write '${' + a variable name + '}')")
            return None
        if key.lower() in seen:
            # Header names are case-insensitive: 'Authorization' + 'authorization' would go
            # out as two lines and the endpoint decides which one wins.
            problems.append(f"{name}: header {key!r} is declared twice (names are "
                            "case-insensitive)")
            return None
        seen.add(key.lower())
        if len(out) >= MAX_HEADERS_PER_SERVER:
            problems.append(f"{name}: at most {MAX_HEADERS_PER_SERVER} header(s) per server")
            return None
        total += len(key) + len(value)
        if total > MAX_HEADERS_TOTAL_CHARS:
            problems.append(f"{name}: headers exceed {MAX_HEADERS_TOTAL_CHARS} characters")
            return None
        out[key] = value
    return out


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

    # 'headers' belongs to the http transport only: a stdio entry has no request to put
    # them on, so accepting one there would be a silent no-op the user believes is set.
    headers = _parse_headers(raw.get("headers"), name, problems)
    if headers is None:
        return None
    if headers and transport != "http":
        problems.append(f"{name}: headers are only meaningful for transport 'http'")
        return None

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
        url=url, headers=headers, roles=role_set, timeout_sec=timeout,
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
