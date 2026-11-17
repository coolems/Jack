"""net_guard - SSRF gate for MCP http endpoints (one job: may this url be contacted?).

An MCP http server is an outbound request to an address the human put in
config/mcp_servers.json. The codebase's single SSRF policy is tools/ssrf_guard.py (the
browser tools use the same classify/grant pair), so this module is a thin adapter:

  * public endpoint          -> allowed, no card;
  * loopback / LAN / metadata / file:// -> refused until the human approves that exact URL
    (the registry asks its 'url' consent card, then approve_endpoint() records the grant);
  * undecidable or hard-blocked target -> refused permanently, a click cannot unlock it.

When tools/ssrf_guard is not importable (bare test context, or a CLIENT that has not
received the shared sources yet) a stdlib classification of the host is used instead, so
the gate never silently disappears: fail-closed on private answers and on DNS failure.

Grants are per-process in-memory only, keyed by the exact URL string, and cleared when the
registry is closed/re-wired. Nothing is persisted.

needs_dns_lookup() tells the transport whether classifying this url would ask DNS, so a
blocking resolver call can be moved off the event loop instead of freezing the UI.
"""

import logging
from typing import Set, Tuple

logger = logging.getLogger("COOLEMS.MCP.NetGuard")

#: Exact URLs the human approved for this process (cleared by clear_grants()).
_approved: Set[str] = set()


def _ssrf_guard():
    """tools.ssrf_guard when importable, else None."""
    try:
        import tools.ssrf_guard as sg
        return sg
    except Exception:
        return None


def needs_dns_lookup(url: str) -> bool:
    """True when classifying *url* would ask DNS (a hostname, not an IP literal).

    Lets the transport decide whether to run the gate in a worker thread: a resolver call
    is blocking, and the gate is consulted before every request, so running it on the
    event loop would freeze the UI for the duration of a slow/timing-out lookup.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse((url or "").strip())
    except Exception:
        return True   # undecidable: treat as the blocking case
    if parsed.scheme not in ("http", "https"):
        return False  # refused by the scheme rule, no DNS involved
    host = parsed.hostname or ""
    if not host:
        return False  # refused without DNS
    try:
        import ipaddress
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
        return False  # an IP literal is classified without DNS
    except ValueError:
        return True


def _embedded_v4(ip):
    """Every IPv4 hidden inside an IPv6 literal (ipv4_mapped, 6to4, Teredo, NAT64).

    '64:ff9b::7f00:1' and a Teredo literal both LOOK global to ipaddress.is_global while
    they carry 127.0.0.1 - the same smuggling tools/ssrf_guard checks for. The fallback
    gate must not be weaker than the guard it stands in for.
    """
    found = []
    try:
        if getattr(ip, "ipv4_mapped", None) is not None:
            found.append(ip.ipv4_mapped)
        if getattr(ip, "sixtofour", None) is not None:
            found.append(ip.sixtofour)
        import ipaddress as _ipa
        if ip in _ipa.IPv6Network("2001::/32"):     # Teredo: client IPv4 in the last 4 bytes
            packed = ip.packed
            found.append(_ipa.IPv4Address(bytes(b ^ 0xFF for b in packed[12:16])))
            found.append(_ipa.IPv4Address(packed[12:16]))
        elif ip in _ipa.IPv6Network("64:ff9b::/96"):  # NAT64: the tail IS the IPv4 address
            found.append(_ipa.IPv4Address(ip.packed[12:16]))
    except (ValueError, IndexError):
        pass  # malformed literal: the caller's is_global check already refused it
    return found


def _literal_refusal(host: str):
    """Reason to refuse an IP-literal host, or None when it is genuinely public.

    Runs on BOTH paths (guard present or not): tools/ssrf_guard covers ipv4-mapped, 6to4
    and Teredo embeds but not the NAT64 well-known prefix, whose last 4 bytes ARE an IPv4
    address - 'http://[64:ff9b::7f00:1]' would reach 127.0.0.1. Checking the literal here
    keeps an MCP endpoint no looser than the strictest policy in the repo.
    """
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None      # a hostname: classification happens elsewhere
    if not ip.is_global:
        kind = ("loopback" if ip.is_loopback else
                "link-local" if ip.is_link_local else
                "multicast" if ip.is_multicast else
                "reserved" if ip.is_reserved else "private")
        return f"endpoint {host} is a {kind} address"
    hidden = [v4 for v4 in _embedded_v4(ip) if not v4.is_global]
    if hidden:
        return f"endpoint {host} hides a private IPv4 address ({hidden[0]})"
    return None


def _stdlib_gate(url: str) -> Tuple[bool, bool, str]:
    """Fallback classification: (ok, consentable, reason). Fail-closed on every error."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    if not isinstance(url, str) or not url.strip():
        return False, False, "MCP http entry has no url"
    try:
        parsed = urlparse(url.strip())
    except Exception as e:
        return False, False, f"unparseable url ({type(e).__name__})"
    if parsed.scheme not in ("http", "https"):
        return False, False, f"only http/https endpoints allowed (got {parsed.scheme!r})"
    host = (parsed.hostname or "").strip()
    if not host:
        return False, False, "no hostname in url"

    refusal = _literal_refusal(host)
    if refusal:
        return False, True, refusal

    try:
        ipaddress.ip_address(host)
    except ValueError:
        # A hostname: resolve ONCE and require every answer to be public.
        try:
            answers = {ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(host, parsed.port or 1)}
        except Exception as e:
            return False, False, f"host {host!r} could not be resolved ({type(e).__name__})"
        if not answers:
            return False, False, f"host {host!r} resolved to nothing"
        if not all(a.is_global for a in answers):
            bad = next(a for a in answers if not a.is_global)
            return False, True, f"host {host!r} resolves to a non-public address ({bad})"
    return True, False, ""


def endpoint_gate(url: str) -> Tuple[bool, bool, str]:
    """May *url* be contacted right now? Returns (ok, consentable, reason).

    ok=True means contact is allowed (public target, or an approved exact URL).
    consentable=True means an explicit user approval can unlock it.
    Never raises: an undecidable target is refused.

    Two checks, in this order:
      1. _literal_refusal() on the host literal - covers NAT64/6to4/Teredo/ipv4-mapped
         embeds, including the NAT64 prefix tools/ssrf_guard does not list;
      2. tools/ssrf_guard when importable, exactly like the browser tools do
         (logic/tool_executor._maybe_ask_url_consent): validate_url_not_ssrff() is the
         POLICY (protocol whitelist, double-decode blocklist, every DNS answer public) and
         classify_blocked_url() only decides whether a refusal is something the human may
         be asked about. Asking only the classifier would skip the policy checks.
    Without the guard, the stdlib fallback classifies the target instead - the gate must
    never silently disappear.
    """
    if isinstance(url, str) and url.strip() in _approved:
        return True, False, ""

    host = ""
    try:
        from urllib.parse import urlparse
        host = urlparse((url or "").strip()).hostname or ""
    except Exception:
        host = ""
    if host:
        refusal = _literal_refusal(host)
        if refusal:
            return False, True, refusal

    sg = _ssrf_guard()
    if sg is not None:
        try:
            safe, why = sg.validate_url_not_ssrff(url)
        except Exception as e:
            return False, False, f"SSRF guard unavailable ({type(e).__name__})"
        if safe:
            return True, False, ""
        try:
            consentable, reason = sg.classify_blocked_url(url)
        except Exception as e:
            return False, False, f"SSRF guard unavailable ({type(e).__name__})"
        # A refusal the classifier cannot name stays hard-blocked (fail-closed).
        return False, bool(consentable), reason or why or "endpoint refused"
    return _stdlib_gate(url)


def approve_endpoint(url: str) -> None:
    """Record the human's approval of this exact endpoint (this process only)."""
    if isinstance(url, str) and url.strip():
        _approved.add(url.strip())
        logger.info("[MCP] endpoint approved by the user for this session")


def clear_grants() -> None:
    """Drop every endpoint grant (registry close / re-wire)."""
    _approved.clear()
