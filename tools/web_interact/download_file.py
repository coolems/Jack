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

import logging
import os
from urllib.parse import unquote
import asyncio

logger = logging.getLogger("COOLEMS.Tools.WebInteract")


async def download_file(url: str, filename: str = None, download_dir: str = "", use_browser_context: bool = True) -> str:
    """
    Download a file from a URL with SSRF protection.

    Args:
        url: The URL to download
        filename: Optional filename (inferred from URL if not provided)
        download_dir: Directory to save to (default: working_root via get_working_root())
        use_browser_context: If True, uses the browser's context for download (handles auth/session)
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
            async with safe_session(timeout=120) as session:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': '*/*'
                }
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        return f"Download failed: HTTP {response.status}"
                    content = await response.read()
                    with open(filepath, 'wb') as f:
                        f.write(content)
                    return f"Downloaded: {filename} ({len(content):,} bytes)"
        except _SSRFError as e:
            logger.error(f"SSRF protection triggered in download_file fallback: {e}")
            return "Error: Access blocked for security."
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Download failed: {e}\n{tb}")
        return f"Download failed: {str(e)}"
