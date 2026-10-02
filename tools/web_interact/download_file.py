"""Download file function - downloads a file from a URL with SSRF protection"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "download_file",
        "description": "Download a file from a URL. Can use browser context for authenticated downloads or direct HTTP fallback.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to download"
                },
                "filename": {
                    "type": "string",
                    "description": "Optional filename (inferred from URL if not provided)"
                },
                "download_dir": {
                    "type": "string",
                    "description": "Directory to save to (default: working_root)",
                    "default": ""
                },
                "use_browser_context": {
                    "type": "boolean",
                    "description": "If True, uses the browser's context for download (handles auth/session)",
                    "default": True
                }
            },
            "required": ["url"]
        }
    }
}

import asyncio
import hashlib
import logging
import os
import re
import time
from urllib.parse import unquote

logger = logging.getLogger("COOLEMS.Tools.WebInteract")

# Full browser-like headers for the direct-HTTP fallback path. Shared by download_file()
# and save_current_image(). Keep this a COMPLETE Chrome UA: truncated UAs ending at
# 'AppleWebKit/537.36' (no '(KHTML, like Gecko) Chrome/x ... Safari/y' tokens) are a
# well-known bot signature that WAFs answer with 403/503 on many sites.
_BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9'
}

# --- WAF JavaScript proof-of-work challenge solving (2026-10) -----------------
# Some sites (e.g. mfe.gov.ro, nginx + JS-challenge WAF edge) answer non-browser
# clients with HTTP 503 and an HTML page that:
#   1. embeds a jsSHA1 library in the body;
#   2. holds an obfuscated string array [prefix, 'cookieName=', 'array'] rotated
#      left N times by a self-executing IIFE (javascript-obfuscator style);
#   3. brute-forces the smallest i where sha1(prefix + str(i)) has two fixed byte
#      values at fixed positions;
#   4. sets document.cookie='<cookieName><prefix><i>; path=/' and reloads -- on
#      reload the edge sees the cookie and serves the real resource.
# A plain HTTP client can never pass that gate by itself, so we REPLICATE the
# page's own algorithm offline (pure hashlib) with the parameters parsed from the
# challenge body, then retry once carrying the solved cookie. This is a bounded,
# deterministic solve -- no JavaScript engine, no extra network probing: exactly
# one challenge GET + one retried GET per download attempt.

# One JS string literal ('...' or "...", backslash-escapes allowed) -- non-capturing core.
_TOK = r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
# The obfuscated 3-string array: ALL THREE tokens are captured so the rotation can be applied.
_CHALLENGE_ARRAY_RE = re.compile(
    r"=\s*\[\s*(" + _TOK + r")\s*,\s*"
    "(" + _TOK + r")\s*,\s*"
    "(" + _TOK + r")\s*\]\s*;")
_CHALLENGE_ROTATE_RE = re.compile(r"_0x4457dc\(\+\+_0x2a548e\)")
# The three a0_0x4457('0xN') lookups, each bound to the value it feeds (whitespace-tolerant:
# real pages are minified with zero spaces, but we must not depend on that):
#   c                  <- prefix (seed for sha1)
#   s1[...]            <- digest method name ('array' -> raw bytes)
#   document.cookie=..+c+i  <- cookie name ('res=')
_CHALLENGE_PREFIX_IDX_RE = re.compile(r"let\s+c\s*=\s*a0_0x4457\(\s*'(0x[0-9a-fA-F]+)'\s*\)")
_CHALLENGE_METHOD_IDX_RE = re.compile(r"s1\s*\[\s*a0_0x4457\(\s*'(0x[0-9a-fA-F]+)'\s*\)\s*\]\s*\(")
_CHALLENGE_COOKIE_IDX_RE = re.compile(
    r"document\s*\[\s*'cookie'\s*\]\s*=\s*a0_0x4457\(\s*'(0x[0-9a-fA-F]+)'\s*\)\s*\+\s*c")
_CHALLENGE_N1_RE = re.compile(r"parseInt\(\s*'0x'\s*\+\s*c\s*\[\s*0x([0-9a-fA-F]+)\s*\]\s*\)")
_CHALLENGE_BYTES_RE = re.compile(
    # target bytes are written as hex literals -- 1 or 2 digits (the real page uses 0xb0 AND 0xb):
    r"s\s*\[\s*n1\s*\]\s*===\s*0x([0-9a-fA-F]{1,2})\s*&&\s*s\s*\[\s*n1\s*\+\s*0x1\s*\]\s*===\s*0x([0-9a-fA-F]{1,2})")

_POW_TIME_BUDGET_S = 5.0      # hard wall-clock cap for the PoW solve (single-threaded)
_POW_CHECK_INTERVAL = 4096    # iterations between budget checks


class _WafChallenge:
    """Parsed parameters of a JS proof-of-work WAF challenge page."""

    __slots__ = ("cookie_name", "prefix", "byte_pos", "byte_lo", "byte_hi")

    def __init__(self, cookie_name, prefix, byte_pos, byte_lo, byte_hi):
        self.cookie_name = cookie_name   # e.g. 'res=' (the document.cookie prefix)
        self.prefix = prefix             # the SHA-1 seed string from the page
        self.byte_pos = byte_pos         # index of the first target byte in the digest
        self.byte_lo = byte_lo           # required value at byte_pos
        self.byte_hi = byte_hi           # required value at byte_pos + 1

    def cookie_value(self, i):
        return f"{self.cookie_name}{self.prefix}{i}"


def _js_unescape(token: str) -> str:
    """Decode a JS single/double-quoted string literal (incl. \\xNN escapes)."""
    inner = token[1:-1]
    out, k = [], 0
    while k < len(inner):
        ch = inner[k]
        if ch == "\\" and k + 1 < len(inner):
            nxt = inner[k + 1]
            if nxt in ("'", '"', "\\"):
                out.append(nxt)
                k += 2
                continue
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "x" and k + 3 < len(inner):
                try:
                    out.append(chr(int(inner[k + 2:k + 4], 16)))
                    k += 4
                    continue
                except ValueError:
                    pass
            # anything else (\\u, \\0, ...): keep literally -- not expected here
            out.append(nxt)
            k += 2
            continue
        out.append(ch)
        k += 1
    return "".join(out)


def parse_waf_challenge(body: bytes) -> "_WafChallenge | None":
    """Parse the mfe.gov.ro-style JS PoW challenge from a WAF HTML body.

    Returns a _WafChallenge with every parameter needed to replicate the page's
    own solve loop, or None when the body does not match the known pattern
    (in which case the caller treats it as an ordinary failure). Pure string work --
    no network access, no JavaScript execution.

    How the obfuscation works: a 3-string literal array is rotated left N times by
    a self-executing IIFE; all later lookups use indices into the ROTATED array.
    We recover each value directly via final[idx] = items[(idx + N) % 3], so neither
    the rotation count nor the original layout has to be guessed -- the lookup
    sites themselves tell us which slot holds the prefix, the method and the cookie.

    The target byte position is also obfuscated: n1 = parseInt('0x' + c[K]) where c
    is the (hex) prefix string -- i.e. n1 is the hex value of the K-th CHARACTER of
    the prefix, not a literal index. We replicate that exactly once the prefix is known.
    """
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return None

    m_rot = _CHALLENGE_ROTATE_RE.search(text)
    if not m_rot:
        return None
    rotate_start = text.rfind("(function(", 0, m_rot.start())
    if rotate_start == -1:
        return None
    segment = text[rotate_start:text.find("};", m_rot.end())]
    calls = re.findall(r"\(\s*a0_0x2a54\s*,\s*(0x[0-9a-fA-F]+)\s*\)", segment)
    if not calls:
        return None
    rotation = int(calls[-1], 16)

    m_prefix = _CHALLENGE_PREFIX_IDX_RE.search(text)
    m_method = _CHALLENGE_METHOD_IDX_RE.search(text)
    m_cookie = _CHALLENGE_COOKIE_IDX_RE.search(text)
    m_n1 = _CHALLENGE_N1_RE.search(text)
    m_bytes = _CHALLENGE_BYTES_RE.search(text)
    if not (m_prefix and m_method and m_cookie and m_n1 and m_bytes):
        return None

    idx_into_prefix = int(m_n1.group(1), 16)   # parseInt('0x' + c[K]) -> K-th char of the prefix
    byte_lo, byte_hi = int(m_bytes.group(1), 16), int(m_bytes.group(2), 16)

    # Try every 3-string array candidate in the page until one validates fully.
    for m_arr in _CHALLENGE_ARRAY_RE.finditer(text):
        items = [_js_unescape(t) for t in m_arr.groups()]
        prefix = items[(int(m_prefix.group(1), 16) + rotation) % 3]
        cookie_name = items[(int(m_cookie.group(1), 16) + rotation) % 3]
        method = items[(int(m_method.group(1), 16) + rotation) % 3]
        if not (method == "array" and re.fullmatch(r"[0-9A-Fa-f]{8,64}", prefix or "")
                and isinstance(cookie_name, str) and cookie_name.endswith("=")
                and len(cookie_name) <= 32):
            continue
        # n1 = parseInt('0x' + c[K]): the hex value of the K-th character of the prefix.
        if not (0 <= idx_into_prefix < len(prefix)) or \
                not re.fullmatch(r"[0-9A-Fa-f]", prefix[idx_into_prefix]):
            continue
        byte_pos = int(prefix[idx_into_prefix], 16)
        return _WafChallenge(cookie_name, prefix, byte_pos, byte_lo, byte_hi)
    return None


def solve_waf_pow(challenge: "_WafChallenge") -> "int | None":
    """Find the smallest i where sha1(prefix + str(i)) matches the target bytes.

    Bounded by a wall-clock budget so an unexpected parameter set can never hang
    the tool; returns None when no solution is found within budget (the caller
    then reports a structured error instead of retrying).
    """
    data = challenge.prefix.encode("utf-8")
    deadline = time.monotonic() + _POW_TIME_BUDGET_S
    i = 0
    while True:
        digest = hashlib.sha1(data + str(i).encode("ascii")).digest()
        if digest[challenge.byte_pos] == challenge.byte_lo and \
                digest[challenge.byte_pos + 1] == challenge.byte_hi:
            return i
        i += 1
        if (i & (_POW_CHECK_INTERVAL - 1)) == 0 and time.monotonic() > deadline:
            logger.warning(f"WAF PoW solve budget ({_POW_TIME_BUDGET_S:.0f}s) exhausted "
                           f"after {i:,} iterations -- giving up")
            return None


def _diagnose(status, content_type: str, body: bytes, redirects: list):
    """Classify a failed download into an actionable category.

    Returns (diagnosis, suggested_action). The diagnosis is written verbatim into
    the tool's error string so an AI reading the response knows what happened and
    what to do next -- no guesswork from a bare status code.
    """
    if _looks_like_challenge(status, content_type, body):
        return "waf_js_pow_challenge", (
            "Server returned a JavaScript proof-of-work bot challenge (HTML page instead of the file). "
            "The built-in offline solver could not pass it for this URL. Retry later; if the block persists, "
            "call download_file again with use_browser_context=true so a real browser executes the challenge JS.")
    if status == 404:
        return "not_found", (
            "URL does not exist on the server (HTTP 404). Verify the URL is current and correctly typed; "
            "the resource may have been moved or deleted. Do NOT retry with different headers -- it will keep returning 404.")
    if status == 410:
        return "gone", "Resource permanently removed (HTTP 410). Find a new URL for the file."
    if status in (401, 403):
        return "auth_or_forbidden", (
            f"Server refused access (HTTP {status}). The resource likely requires login or blocks non-browser clients. "
            "Retry with use_browser_context=true to carry an authenticated browser session.")
    if status == 429:
        return "rate_limited", (
            "Rate limited by the server (HTTP 429). Wait a few minutes and retry; reduce request frequency.")
    if status in (500, 502):
        return "server_error", (
            f"Upstream/server error (HTTP {status}). The origin is temporarily broken -- retry later. "
            "Do not hammer it repeatedly.")
    if status == 503:
        return ("service_unavailable_or_ip_blocked",
                "Server edge answered 503 with a plain (non-challenge) body -- typically an IP-level block or the WAF failing open. "
                "Retrying from this machine will keep failing; try later, from another network, or use use_browser_context=true.")
    if status == 504:
        return "gateway_timeout", "Upstream timed out (HTTP 504). Retry later."
    return f"http_{status}", f"Unusual HTTP {status} -- inspect the body snippet in this error for clues; retry once, then treat as a permanent failure if it repeats."


def _looks_like_challenge(status: int, content_type: str, body: bytes) -> bool:
    """Cheap pre-filter before parsing: only non-200 HTML bodies are candidates."""
    return (status != 200 and isinstance(body, (bytes, bytearray)) and len(body) >= 512
            and "html" in (content_type or "").lower())


def _format_error(url: str, final_url: str, status, content_type: str, body: bytes,
                  redirects: list, diagnosis: str, suggested_action: str) -> str:
    """Build the structured, AI-readable failure message returned by download_file()."""
    try:
        preview = (body or b"").decode("utf-8", errors="replace")[:300].strip()
    except Exception:
        preview = "<binary body>"
    chain = " -> ".join([url] + redirects) if redirects else url
    return ("Download failed [diagnosis={}]: HTTP {} for {}.\n"
            "  final_url: {}\n"
            "  content_type: {}\n"
            "  server_body_preview: {!r}\n"
            "  redirect_chain: {}\n"
            "  suggested_action: {}".format(
                diagnosis, status, url, final_url or url,
                content_type or "(none)", preview, chain, suggested_action))


async def _http_get_once(session, url: str, headers: dict) -> dict:
    """One GET through the SSRF-pinned session. Returns status/final URL/content type/body/redirects.

    'redirects' holds each hop's DESTINATION (its Location header), so the reported chain reads
    original -> dest1 -> dest2 ... and ends at final_url -- exactly what an AI reader needs to see
    where a download actually went before it failed.
    """
    async with session.get(url, headers=headers) as response:
        body = await response.read()
        redirects = []
        for h in (response.history or []):
            dest = (h.headers or {}).get("Location") or str(h.url)
            redirects.append(dest)
        return {
            "status": response.status,
            "final_url": str(response.url),
            "content_type": (response.headers or {}).get("Content-Type", ""),
            "body": body,
            "redirects": redirects,
        }


async def download_file(url: str, filename: str = None, download_dir: str = "", use_browser_context: bool = True) -> str:
    """
    Download a file from a URL with SSRF protection.

    Args:
        url: The URL to download
        filename: Optional filename (inferred from URL if not provided)
        download_dir: Directory to save to (default: working_root via get_working_root())
        use_browser_context: If True, uses the browser's context for download (handles auth/session)

    Returns:
        Success: "Downloaded: <filename> (<N> bytes)" (+ " (via WAF challenge solved)" when a JS
                 proof-of-work gate was detected and passed offline).
        Failure: a structured multi-line message starting with "Download failed [diagnosis=...]"
                 that carries the HTTP status, final URL after redirects, content type, a body
                 snippet, the redirect chain, and an explicit suggested_action so an AI reading
                 the result can decide the next step (retry later / use browser context / fix URL).
    """
    from ..utils import get_working_root, resolve_path_to_dir, validate_filename

    # Normalize URL scheme (2026-07-15): file:// stays INTACT for the SSRF gate's
    # consent classification; https:// auto-added only to bare hostnames.
    if not url.startswith(('http://', 'https://', 'file://')):
        url = 'https://' + url

    # --- SSRF Protection (fail-closed, no fallbacks — 2026-08-28 hardening;
    # the old weaker regex fallback was removed on purpose) ---
    try:
        from tools.ssrf_guard import ensure_url_not_ssrff
        ensure_url_not_ssrff(url)
    except ImportError as e:
        logger.error(f"SSRF defense module unavailable in download_file, refusing to proceed (no fallback): {e!r}")
        return "Error: SSRF validation unavailable — request refused."
    except Exception as e:  # includes SSRFError and any unexpected failure -> hard stop
        logger.error(f"SSRF validation failed in download_file, refusing to proceed (no fallback): {e!r}")
        return f"Error: Access blocked for security. ({str(e)[:200]})"
    # Resolve effective download directory -- SECURITY (2026-07-15): every path
    # is locked inside working_root. User-supplied download_dir/filename are
    # validated before use; there is NO os.getcwd() fallback anymore.
    try:
        wr = get_working_root()
    except Exception as e:
        return f"Error: working_root not configured, cannot resolve a safe download location ({e})."

    if download_dir and download_dir.strip():
        try:
            download_dir = resolve_path_to_dir(wr, download_dir)
        except ValueError as e:
            return f"Error: {e}"
    else:
        download_dir = wr

    # Infer filename from URL when not provided (basename only -- safe by construction)
    if not filename or not filename.strip():
        candidate = unquote(url.split('/')[-1].split('?')[0])
        if candidate and '.' in candidate:
            filename = os.path.basename(candidate)
        else:
            filename = f"download_{int(asyncio.get_event_loop().time())}.bin"

    # SECURITY (2026-07-15): user-supplied filenames may contain separators or '..'
    # (e.g. '../../evil.txt') -- validate before joining into the directory.
    try:
        validate_filename(filename, label="Download filename")
    except ValueError as e:
        return f"Error: {e}"

    os.makedirs(download_dir, exist_ok=True)
    filepath = os.path.normpath(os.path.join(download_dir, filename))

    # Final containment check -- belt and braces (realpath resolves symlinks/..)
    from ..utils import guard_path_inside_working_root
    try:
        guard_path_inside_working_root(filepath, wr, label="Download file path")
    except ValueError as e:
        return f"Error: {e}"

    try:
        # Try browser context download first if requested
        if use_browser_context:
            from .shared_instance import get_web_interact
            wi = get_web_interact()
            if wi and wi.page:
                try:
                    async with wi.page.context.expect_download(timeout=30000) as download_info:
                        await wi.page.goto(url)
                    download = await download_info.value
                    suggested = download.suggested_filename or filename
                    await download.save_as(filepath)
                    return f"Downloaded: {suggested} (via browser)"
                except Exception as e:
                    logger.warning(f"Browser download failed, falling back to HTTP: {e}")

        # Fallback to direct HTTP request -- SSRF-pinned (2026-09): every connection,
        # including redirect hops and IP-literal targets, is resolved once, validated
        # against private/reserved ranges, and pinned inside the connector. The old
        # bare ClientSession() here had NO pinning at all -- that gap is closed now.
        from tools.ssrf_guard import safe_session, SSRFError as _SSRFError
        try:
            # Full browser-like headers (2026-09 debug): the previous TRUNCATED UA
            # ('...AppleWebKit/537.36' with no Chrome token) is a known bot signature --
            # WAFs answer 403/503 for it on many sites. Same complete-Chrome-UA approach
            # as tools/web_tools/fetch_url.py (which rotates this Windows UA + a Mac variant).
            headers = dict(_BROWSER_HEADERS)

            async def _one_get(extra_headers=None):
                h = dict(headers)
                if extra_headers:
                    h.update(extra_headers)
                async with safe_session(timeout=120) as session:
                    return await _http_get_once(session, url, h)

            result = await _one_get()
            status = result["status"]
            waf_note = ""

            # --- WAF JS proof-of-work gate (2026-10): detect the challenge in a
            # non-200 HTML body, solve its SHA-1 PoW offline with the parameters
            # parsed from the page itself, then retry ONCE carrying the cookie.
            if status != 200 and _looks_like_challenge(status, result["content_type"], result["body"]):
                challenge = parse_waf_challenge(result["body"])
                if challenge is not None:
                    logger.info(f"WAF JS PoW challenge detected (HTTP {status}) for {url} -- solving offline")
                    i = solve_waf_pow(challenge)
                    if i is not None:
                        cookie_value = challenge.cookie_value(i)
                        logger.info(f"WAF PoW solved in-process: i={i:,} cookie={cookie_value!r}")
                        result2 = await _one_get({"Cookie": cookie_value})
                        waf_note = " (via WAF challenge solved)"
                        if result2["status"] == 200:
                            status, result = result2["status"], result2
                        else:
                            # Solved but the edge still refused -- report the SECOND answer.
                            diagnosis, action = _diagnose(
                                result2["status"], result2["content_type"], result2["body"], result2["redirects"])
                            return (_format_error(url, result2["final_url"], result2["status"],
                                                  result2["content_type"], result2["body"],
                                                  result2["redirects"], diagnosis, action)
                                    + f"\n  note: WAF PoW was solved (i={i:,}) but the retried request still failed.")

            def _save_and_report(content: bytes, note: str = "") -> str:
                with open(filepath, 'wb') as f:
                    f.write(content)
                return f"Downloaded: {filename} ({len(content):,} bytes){note}"

            if status == 200:
                return _save_and_report(result["body"], waf_note)

            # Transient failures (WAF blip 503, rate-limit 429, 502/504): one polite retry after a pause.
            last_status = status
            if last_status in (429, 500, 502, 503, 504):
                logger.warning(f"Transient HTTP {last_status}, retrying download once after 2s: {url}")
                await asyncio.sleep(2)
                result = await _one_get()
                last_status = result["status"]

            if last_status == 200:
                return _save_and_report(result["body"])   # recovered on the retry -- still a success

            diagnosis, action = _diagnose(last_status, result["content_type"], result["body"], result["redirects"])
            return _format_error(url, result["final_url"], last_status, result["content_type"],
                                 result["body"], result["redirects"], diagnosis, action)

        except _SSRFError as e:
            logger.error(f"SSRF protection triggered in download_file fallback: {e}")
            return "Error: Access blocked for security."
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Download failed: {e}\n{tb}")
        return f"Download failed [diagnosis=network_error]: {str(e)}\n  suggested_action: Check connectivity/DNS and retry; if it persists the host may be unreachable from this network."
