"""ssrf_guard -- professional SSRF defense for asyncio HTTP clients. SINGLE SOURCE OF TRUTH.

Rewrite of tools/ssrf_defense.py (2026-09). The old module closed the DNS-rebinding /
TOCTOU window by monkeypatching socket.getaddrinfo for the duration of a request:
global, process-wide state with non-atomic save/restore that could not be made safe
under concurrency (a second overlapping pin destroyed or leaked the first -- a live
SSRF bypass). That mechanism is GONE.

THE PRINCIPLE (why this design is bulletproof)
    The IP that gets validated must be the SAME ip the socket connects to, by
    construction. PinnedConnector resolves each hostname exactly ONCE, validates every
    answer against the private/reserved policy below, and then feeds ONLY those exact
    addresses to aiohttp's connector. There is no second resolution -- not at connect
    time, not on redirects, not after a cache expiry -- so there is no window to bind
    into. DNS rebinding is eliminated by construction, not policed afterwards.

WHAT THIS MODULE CONTAINS
  * IP policy (Layer 2 core) -- private/reserved classification incl. IPv6 embedded-IPv4
    notations (v4-mapped / 6to4 / Teredo). Fail-closed: unparseable input is blocked.
  * URL validation API -- validate_url_not_ssrff() / ensure_url_not_ssrff(): protocol
    whitelist, double-decode substring block list, IP-literal classification, and a
    single resolution check for hostnames. Used as the pre-navigation GATE by browser
    tools (check_url/goto) where Chrome does its own DNS and we cannot pin it from
    Python.
  * PinnedConnector -- an aiohttp.TCPConnector subclass that enforces the principle at
    the transport layer: every hostname (initial URL AND every redirect hop, literal or
    name) is resolved once, validated, and pinned for the lifetime of the session.
  * safe_session() -- one-line factory producing a fully guarded ClientSession.

ASYNCIO-ONLY CONTRACT
    No threads are spawned anywhere in this module and no threading primitives are used.
    All state (pin cache, in-flight dedup) lives on per-session connector objects owned
    by one event loop; dict operations between awaits are atomic on a single loop, so
    no locks exist or are needed. The only executor involvement is asyncio's own
    non-blocking DNS offload (loop.getaddrinfo), which is part of the event loop
    machinery itself -- our code never creates a worker thread.

FAIL-CLOSED CONTRACT (no fallbacks)
    If validation is unavailable or fails in ANY way, the request is BLOCKED and an
    error is logged. There is no silent pass-through path anywhere in this module:
      * unparseable IP literal          -> blocked
      * hostname resolving to ANY private/reserved/embedded address -> blocked
      * DNS failure / empty answer set  -> blocked
      * redirect target that is a blocked IP literal -> blocked (caught at connect time,
        which the old redirect-hook design had to special-case)

DELIVERY NOTE (server/client split)
    This file is the single source of truth and is delivered to CLIENTs with every
    tools_response (see app/providers/coolems/tool_scanner/shared_sources.py::get_all_shared_sources
    and tools_server/local_tool_scanner.py -- 'ssrf_guard' MUST stay in both lists,
    ordered BEFORE 'ssrf_defense'). The old name tools/ssrf_defense.py remains as a
    thin deprecation shim so already-delivered tool code keeps working.

CLIENT usage requires aiohttp (listed in CLIENT/requirements.txt). Browser navigation
tools do NOT need it -- they only use the sync-free validation gate above.
"""

import asyncio
import ipaddress
import logging
import socket
from typing import Callable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

logger = logging.getLogger("COOLEMS.SSRFGuard")


# ---------------------------------------------------------------------------
# Layer 2 core -- private / reserved network ranges (blocked for SSRF protection)
# ---------------------------------------------------------------------------
_BLOCKED_NETWORKS_V4: list = [
    ipaddress.IPv4Network('127.0.0.0/8'),      # Loopback
    ipaddress.IPv4Network('0.0.0.0/8'),        # Current network
    ipaddress.IPv4Network('10.0.0.0/8'),       # Private Class A
    ipaddress.IPv4Network('172.16.0.0/12'),    # Private Class B
    ipaddress.IPv4Network('192.168.0.0/16'),   # Private Class C
    ipaddress.IPv4Network('169.254.0.0/16'),   # Link-local (AWS metadata)
    ipaddress.IPv4Network('100.64.0.0/10'),    # Carrier-grade NAT
    ipaddress.IPv4Network('198.18.0.0/15'),    # Benchmark testing
    ipaddress.IPv4Network('224.0.0.0/4'),      # Multicast
    ipaddress.IPv4Network('240.0.0.0/4'),      # Reserved
]

_BLOCKED_NETWORKS_V6: list = [
    ipaddress.IPv6Network('::1/128'),          # IPv6 Loopback
    ipaddress.IPv6Network('::/128'),           # Unspecified
    ipaddress.IPv6Network('fc00::/7'),         # Unique local
    ipaddress.IPv6Network('fe80::/10'),        # Link-local
    ipaddress.IPv6Network('ff00::/8'),         # Multicast
]

_TEREDO_PREFIX = ipaddress.IPv6Network('2001::/32')   # RFC 4380 Teredo service prefix

# Layer 1: block list for substring check on fully decoded URL
_BLOCKED_STRINGS = [
    'localhost', '127.0.0.1', '0.0.0.0', 'file://',
    '::1', '[::1]', '0:0:0:0:0:0:0:1'
]


# ---------------------------------------------------------------------------
# IP classification (fail-closed)
# ---------------------------------------------------------------------------
def _extract_embedded_v4(ipv6: ipaddress.IPv6Address) -> List[ipaddress.IPv4Address]:
    """Return every IPv4 address embedded inside an IPv6 literal.

    Covers the three notations that can hide a private v4 behind a public-looking
    v6 literal (all previously passed the old v6-only check -- 2026-08-28 fix):
      * ::ffff:a.b.c.d   -> stdlib .ipv4_mapped
      * 2002:<v4>::/48   -> stdlib .sixtofour
      * Teredo 2001::/32 -> RFC 4380 sec. 4: last 4 bytes = client IPv4, always
        bit-reversed (XOR 0xFFFFFFFF) per the RFC's obfuscation rule
    """
    found: List[ipaddress.IPv4Address] = []

    if ipv6.ipv4_mapped is not None:
        found.append(ipv6.ipv4_mapped)
    if ipv6.sixtofour is not None:
        found.append(ipv6.sixtofour)

    try:
        if ipv6 in _TEREDO_PREFIX:
            packed = ipv6.packed          # 16 bytes, RFC 4380 sec. 4 layout:
            #   [0:4]   Prefix (2001::/32)
            #   [4:8]   Server IPv4
            #   [8:10]  Flags (cone bit = MSB; does NOT toggle obfuscation)
            #   [10:12] Port (obfuscated)
            #   [12:16] Client IPv4 (obfuscated -- always XOR 0xFFFFFFFF)
            client_bytes = packed[12:16]
            # Defense-in-depth: check BOTH interpretations (spec-mandated XOR and the
            # raw bytes) so a malformed/legacy Teredo literal cannot smuggle a private v4.
            found.append(ipaddress.IPv4Address(bytes(b ^ 0xFF for b in client_bytes)))
            found.append(ipaddress.IPv4Address(client_bytes))
    except (ValueError, IndexError):
        pass  # malformed packed data -- the outer v6 check still applies

    return found


def is_blocked_ip_obj(ip) -> bool:
    """True if *ip* falls into a blocked range OR embeds a blocked IPv4 address."""
    networks = (_BLOCKED_NETWORKS_V4 if isinstance(ip, ipaddress.IPv4Address)
                else _BLOCKED_NETWORKS_V6)
    for net in networks:
        if ip in net:
            return True

    # Embedded-IPv4 notations (mapped / 6to4 / Teredo): the outer v6 may look public
    # while carrying a private v4 -- inspect every embed.
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in _extract_embedded_v4(ip):
            if is_blocked_ip_obj(embedded):
                return True
    return False


def is_private_ip(ip_str: str) -> bool:
    """Check whether an IP string falls into private/reserved ranges.

    Handles IPv6 literals that embed a private IPv4 (mapped, 6to4, Teredo).
    FAIL-CLOSED: unparseable input returns True (blocked) -- this function is only
    ever called with literal-IP strings; hostnames are resolved first by the caller.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        logger.error(f"SSRF guard: cannot parse IP literal, blocking fail-closed: {ip_str!r}")
        return True
    return is_blocked_ip_obj(ip)


# Backward-compatible alias (old name).
_is_private_ip = is_private_ip


def _parse_host_literal(host: str):
    """Parse *host* as an IP literal. Returns the ip_address object or None.

    Strips IPv6 zone ids ('fe80::1%eth0') and brackets defensively; anything that does
    not parse as a canonical IP is NOT a literal (it's a hostname).
    """
    if not host:
        return None
    h = host.strip().rstrip('.')
    if h.startswith('[') and h.endswith(']'):
        h = h[1:-1]
    if '%' in h:
        h = h.split('%', 1)[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        return None


def is_localhost_ip(host: str) -> bool:
    """Check whether *host* resolves to a localhost / loopback address.

    Standalone utility kept for backward compatibility (the CLIENT's boot-time auth
    keeps its own stdlib-only copy). NOT part of the SSRF validation path -- use
    validate_url_not_ssrff() instead.
    """
    if host in ("127.0.0.1", "::1", "localhost"):
        return True

    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        pass

    try:
        results = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for res in results:
            ip_str = res[4][0]
            try:
                if ipaddress.ip_address(ip_str).is_loopback:
                    return True
            except ValueError:
                continue
    except OSError:
        pass

    return False


# ---------------------------------------------------------------------------
# URL validation API (sync, no I/O side effects beyond one optional resolution)
# ---------------------------------------------------------------------------
class SSRFError(Exception):
    """Raised when a URL fails SSRF validation or the pinned-IP contract is violated."""


def _hostname_of(url: str) -> str:
    parsed = urlparse(unquote(unquote(url)).lower())
    return (parsed.hostname or '').strip()


def validate_url_not_ssrff(
    url: str,
    resolve_hook: Optional[Callable[[str], List[str]]] = None,
) -> Tuple[bool, str]:
    """Validate that a URL does not target internal / private addresses.

    Defense-in-depth with 3 layers:
      Layer 0 -- Protocol whitelist (only http/https allowed)
      Layer 1 -- Double URL-decode + substring block list check
      Layer 2 -- Target classification, fail-closed on ANY error:
                  * IP literals are classified directly (no DNS involved);
                  * hostnames are resolved ONCE and every answer must be public.

    Args:
        url: The URL to validate.
        resolve_hook: Optional callable hostname -> list of IP strings for the Layer-2
            resolution step. When None, socket.getaddrinfo is used (a single sync call --
            appropriate for the pre-navigation GATE in browser tools).

    Returns:
        Tuple of (is_safe, reason_string). is_safe=True means URL passed all checks.
    """
    # --- Layer 0: Protocol whitelist ---
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        return False, "Only HTTP/HTTPS protocols allowed"

    # --- Layer 1: Double-decode + substring block check ---
    decoded_url = unquote(unquote(url)).lower()
    for blocked in _BLOCKED_STRINGS:
        if blocked in decoded_url:
            return False, f"Blocked hostname detected: {blocked}"

    # --- Layer 2: classify the target (fail-closed on ANY error) ---
    hostname = _hostname_of(url)
    if not hostname:
        return False, "No hostname in URL"

    literal = _parse_host_literal(hostname)
    if literal is not None:
        # IP literal -- no DNS involved; classify directly. This closes the legacy gap
        # where 'http://127.0.0.1/' only died at Layer 1 by substring luck.
        if is_blocked_ip_obj(literal):
            logger.error(f"SSRF guard: URL targets blocked IP literal {hostname}")
            return False, f"URL targets private/reserved IP: {hostname}"
        return True, "OK"

    # Hostname -- resolve ONCE and require every answer to be public.
    if resolve_hook is None:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except Exception as e:  # FAIL-CLOSED (2026-08-28 contract): no pass-through on errors
            logger.error(f"SSRF guard: DNS resolution failed for {hostname!r}, "
                         f"blocking fail-closed ({e!r})")
            return False, f"DNS resolution failed for: {hostname}"
        ips = [res[4][0] for res in infos]
    else:
        try:
            ips = list(resolve_hook(hostname)) or []
        except Exception as e:  # FAIL-CLOSED (2026-08-28 contract)
            logger.error(f"SSRF guard: resolver hook failed for {hostname!r}, "
                         f"blocking fail-closed ({e!r})")
            return False, f"DNS resolution failed for: {hostname}"

    if not ips:
        logger.error(f"SSRF guard: no addresses resolved for {hostname!r}, blocking fail-closed")
        return False, "No addresses resolved for hostname"

    for ip_str in ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str.split('%', 1)[0])
        except ValueError:
            logger.error(f"SSRF guard: unparseable address {ip_str!r} for {hostname!r}, "
                         f"blocking fail-closed")
            return False, f"Unparseable resolved address: {ip_str}"
        if is_blocked_ip_obj(ip_obj):
            logger.error(f"SSRF guard: {hostname!r} resolved to private/reserved IP {ip_str}, blocking")
            return False, f"Resolved to private/reserved IP: {ip_str} (hostname: {hostname})"

    return True, "OK"


def ensure_url_not_ssrff(
    url: str,
    resolve_hook: Optional[Callable[[str], List[str]]] = None,
) -> None:
    """Strict variant of validate_url_not_ssrff() -- raises instead of returning.

    Raises:
        SSRFError with the block reason when validation fails in ANY way (invalid
        protocol, blocked host, private/reserved/embedded IP, DNS failure, or an
        unexpected internal error). Logs at ERROR level on every denial.

    On success returns None and logs nothing. There is no fallback path: callers must
    treat an SSRFError as a hard stop (refuse the request, do not retry).
    """
    safe, reason = validate_url_not_ssrff(url, resolve_hook=resolve_hook)
    if not safe:
        logger.error(f"SSRF guard [strict]: URL blocked -- {reason} -- url={url}")
        raise SSRFError(f"{reason} (url={url})")


# ---------------------------------------------------------------------------
# Transport-layer enforcement for aiohttp: PinnedConnector + safe_session()
# ---------------------------------------------------------------------------
def _import_aiohttp():
    """Import aiohttp lazily so the validation gate works in environments without it."""
    try:
        import aiohttp
        return aiohttp
    except ImportError as e:  # pragma: no cover - environment guard
        raise SSRFError(
            "aiohttp is required for safe_session()/PinnedConnector (install per requirements.txt)"
        ) from e


def _pinned_connector_base():
    """Return TCPConnector, or a fail-closed placeholder when aiohttp is absent.

    The placeholder keeps this module importable in environments without aiohttp
    (e.g. the CLIENT before requirements install): only safe_session()/PinnedConnector
    need it; the validation gate works with stdlib alone. Instantiating the
    placeholder raises SSRFError -- fail-closed, no silent degradation.
    """
    try:
        return _import_aiohttp().TCPConnector
    except SSRFError:
        class _NoAiohttpBase:
            def __init__(self, *args, **kwargs):
                raise SSRFError(
                    "aiohttp is required for PinnedConnector/safe_session (install per requirements.txt)"
                ) from None
        return _NoAiohttpBase


class PinnedConnector(_pinned_connector_base()):
    """aiohttp connector that makes SSRF pinning a property of the connection itself.

    Overrides _resolve_host() -- the SINGLE point through which every new TCP
    connection (initial URL, reconnects, and every redirect hop) obtains its target
    addresses:

      * IP-literal hosts are classified directly and blocked if private/reserved
        (aiohttp's own resolver would short-circuit literals without any policy check);
      * hostnames are resolved exactly ONCE via the loop's non-blocking getaddrinfo,
        every answer is validated against the Layer-2 policy, and the resulting address
        list is PINNED on this connector for its lifetime. Later connections to the same
        host reuse the pinned set -- DNS is never asked again, so a rebinding attack's
        second answer can never be seen or used;
      * any failure (DNS error, empty set, private answer) raises SSRFError BEFORE a
        socket exists. Nothing is pinned on failure, so a later request may retry the
        resolution fresh.

    Per-session state only: two sessions never share pins, and all dict operations run
    on one event loop between awaits -- no locks, no threads, no global state.

    TLS stays correct by construction: aiohttp connects to our pinned IP but performs
    SNI + certificate verification against the ORIGINAL hostname (see
    TCPConnector._create_direct_connection: server_hostname = req.server_hostname or host).
    """

    def __init__(self, *args, dns_lookup=None, **kwargs):
        # Our own pin cache replaces aiohttp's TTL DNS cache for this connector.
        kwargs.setdefault('use_dns_cache', False)
        super().__init__(*args, **kwargs)
        self._pins: dict = {}          # normalized host -> pinned address entries
        self._inflight: dict = {}      # normalized host -> in-flight resolution future
        # Injectable for tests; default is the loop's own non-blocking DNS offload.
        self._lookup = dns_lookup or self._default_lookup

    async def _default_lookup(self, host: str, port: int) -> list:
        loop = asyncio.get_running_loop()
        return await loop.getaddrinfo(
            host, port or 0, type=socket.SOCK_STREAM, family=socket.AF_UNSPEC)

    # -- the single enforcement point --------------------------------------
    async def _resolve_host(self, host: str, port: int, traces=None):
        key = (host or '').strip().rstrip('.').lower()

        pinned = self._pins.get(key)
        if pinned is not None:
            return pinned                      # validated once -- never re-resolve

        literal = _parse_host_literal(key)
        if literal is not None:
            # IP-literal target (initial URL or redirect): classify directly. aiohttp's
            # stock path would connect to it with zero policy involvement.
            if is_blocked_ip_obj(literal):
                logger.error(f"SSRF guard [pin]: blocked IP-literal target {key!r}")
                raise SSRFError(f"Blocked target: private/reserved IP literal {key!r}")
            entry = [{
                "hostname": host, "host": key, "port": port,
                "family": self._family, "proto": 0, "flags": 0,
            }]
            self._pins[key] = entry
            return entry

        # Hostname: dedupe concurrent first-resolves on this session (single loop).
        fut = self._inflight.get(key)
        if fut is not None:
            return await fut

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[key] = fut
        try:
            entries = await self._do_resolve(host, port, key)
        except BaseException as exc:
            if not fut.cancelled():
                fut.set_exception(exc)         # waiters re-raise; nothing pinned on failure
                fut.exception()                # mark retrieved: no GC-time 'never retrieved' noise when there are no waiters
            raise
        finally:
            self._inflight.pop(key, None)

        self._pins[key] = entries              # pin for the lifetime of this session
        fut.set_result(entries)
        return entries

    async def _do_resolve(self, host: str, port: int, key: str) -> list:
        try:
            infos = await self._lookup(host, port or 0)
        except Exception as e:  # gaierror & friends -- FAIL-CLOSED
            logger.error(f"SSRF guard [pin]: DNS resolution failed for {host!r}: {e!r}")
            raise SSRFError(f"DNS resolution failed for {host!r}") from e

        entries = []
        for family, _socktype, proto, _canonname, sockaddr in infos:
            ip_str = str(sockaddr[0])
            try:
                ip_obj = ipaddress.ip_address(ip_str.split('%', 1)[0])
            except ValueError:
                logger.error(f"SSRF guard [pin]: unparseable address {ip_str!r} for {host!r}")
                raise SSRFError(
                    f"Unparseable resolved address {ip_str!r} for {host!r}") from None
            if is_blocked_ip_obj(ip_obj):
                logger.error(f"SSRF guard [pin]: {host!r} resolved to private/reserved IP "
                             f"{ip_str}, blocking")
                raise SSRFError(
                    f"{host!r} resolved to private/reserved IP: {ip_str}")
            entries.append({
                "hostname": host, "host": ip_str, "port": port,
                "family": family, "proto": proto, "flags": 0,
            })

        if not entries:
            logger.error(f"SSRF guard [pin]: no addresses resolved for {host!r}")
            raise SSRFError(f"No addresses resolved for {host!r}")
        return entries


def safe_session(timeout=30.0, **connector_kwargs):
    """Create a fully SSRF-guarded aiohttp.ClientSession (asyncio-only).

    Every connection made through this session -- including all redirect hops and any
    IP-literal target -- passes PinnedConnector's resolve-once-validate-pin contract.
    Callers should still run ensure_url_not_ssrff() on the initial URL for a fast,
    logged pre-check; the connector is the enforcement layer that cannot be bypassed by
    redirects or re-resolution.

    Args:
        timeout: Total request budget -- seconds (float) or an aiohttp.ClientTimeout.
        **connector_kwargs: Forwarded to PinnedConnector/TCPConnector (ssl, limit, ...).

    Returns:
        An open ClientSession -- use as an async context manager ('async with').
    """
    aiohttp = _import_aiohttp()
    if isinstance(timeout, aiohttp.ClientTimeout):
        client_timeout = timeout
    else:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
    connector = PinnedConnector(**connector_kwargs)
    return aiohttp.ClientSession(connector=connector, timeout=client_timeout)
