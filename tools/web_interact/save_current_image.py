"""Save current image function - saves the currently displayed image from the browser page"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "save_current_image",
        "description": "Save the currently displayed image from the browser page to the working_root folder. Auto-detects main image via DOM, background, or direct URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Optional filename for saved image"},
                "download_dir": {"type": "string", "description": "Directory to save to (default: working_root)", "default": ""}
            },
            "required": []
        }
    }
}

import logging
import os
import datetime
from urllib.parse import unquote

logger = logging.getLogger("COOLEMS.Tools.WebInteract")

async def save_current_image(filename: str = None, download_dir: str = "") -> str:
    """
    Save the currently displayed image from the browser page.
    This is useful for sites that block direct downloads but display images.
    """
    from .ensure_connected import ensure_connected
    from .shared_instance import get_web_interact
    from ..utils import get_working_root, resolve_path_to_dir, validate_filename, guard_path_inside_working_root

    if not await ensure_connected():
        return "❌ Not connected to Chrome."

    from .shared_instance import ensure_page
    await ensure_page()  # self-heal: re-acquire or create a live page (2026-09-14 alias fix: CLIENT transform neutralizes same-package imports; aliases are never injected into exec globals -> NameError)
    wi = get_web_interact()
    if not wi or wi.page is None:
        return "❌ No page available."
    
    try:
        # Find the main image on the page
        image_url = None
        
        # Strategy 1: Look for img tag that's likely the main content
        candidates = await wi.page.evaluate("""() => {
            const images = document.querySelectorAll('img');
            let best = null;
            let bestScore = 0;
            
            for (const img of images) {
                let score = 0;
                const src = img.src || '';
                const width = img.width || img.naturalWidth || 0;
                const height = img.height || img.naturalHeight || 0;
                
                if (width > 800) score += 10;
                if (height > 600) score += 10;
                
                const rect = img.getBoundingClientRect();
                if (rect.width > 100 && rect.height > 100) score += 5;
                
                if (img.alt && img.alt.length > 5) score += 3;
                
                if (src.includes('logo') || src.includes('icon')) score -= 20;
                if (src.includes('thumb')) score -= 10;
                
                if (score > bestScore) {
                    bestScore = score;
                    best = img;
                }
            }
            
            return best ? { src: best.src, width: best.width, height: best.height } : null;
        }""")
        
        if candidates and candidates.get('src'):
            image_url = candidates['src']
            logger.info(f"[SAVE_IMAGE] Found image via DOM: {image_url}")
        
        # Strategy 2: If no suitable img tag, try to get background image
        if not image_url:
            background = await wi.page.evaluate("""() => {
                const elements = document.querySelectorAll('[style*="background-image"]');
                for (const el of elements) {
                    const style = getComputedStyle(el);
                    const bg = style.backgroundImage;
                    if (bg && bg !== 'none') {
                        const match = bg.match(/url\\(["']?([^"')]+)["']?\\)/);
                        if (match) return match[1];
                    }
                }
                return null;
            }""")
            if background:
                image_url = background
                logger.info(f"[SAVE_IMAGE] Found background image: {image_url}")
        
        # Strategy 3: Use the page's URL if it points to an image
        if not image_url:
            current_url = wi.page.url
            if current_url.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                image_url = current_url
                logger.info(f"[SAVE_IMAGE] Using page URL as image: {image_url}")
        
        if not image_url:
            return "❌ No image found on current page. Make sure you're on a page that displays an image."
        
        # Resolve effective download directory -- SECURITY (2026-07-15): every
        # path is locked inside working_root. User-supplied download_dir/filename
        # are validated before use; there is NO unchecked os.path.join anymore.
        wr = get_working_root()

        if download_dir and download_dir.strip():
            try:
                download_dir = resolve_path_to_dir(wr, download_dir)
            except ValueError as e:
                return f"❌ Failed to save image: {e}"
        else:
            download_dir = wr

        # Generate filename if not provided (URL-inferred name is basename-only -- safe by construction)
        if not filename or not filename.strip():
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            url_filename = os.path.basename(unquote(image_url.split('/')[-1].split('?')[0]))
            if url_filename and '.' in url_filename:
                filename = url_filename
            else:
                filename = f"image_{timestamp}.jpg"

        # SECURITY (2026-07-15): user-supplied filenames may contain separators or '..'
        try:
            validate_filename(filename, label="Image filename")
        except ValueError as e:
            return f"❌ Failed to save image: {e}"

        os.makedirs(download_dir, exist_ok=True)
        filepath = os.path.normpath(os.path.join(download_dir, filename))

        # Final containment check -- belt and braces (realpath resolves symlinks/..)
        try:
            guard_path_inside_working_root(filepath, wr, label="Image file path")
        except ValueError as e:
            return f"❌ Failed to save image: {e}"
        
        # Method 1: Try to get the image via Playwright's built-in screenshot
        try:
            if candidates and candidates.get('src'):
                img_element = await wi.page.query_selector(f'img[src="{image_url}"]')
                if img_element:
                    await img_element.screenshot(path=filepath)
                    file_size = os.path.getsize(filepath)
                    return f"✅ Image saved successfully!\n📁 File: {filename}\n📂 Path: {filepath}\n📊 Size: {file_size:,} bytes\n🔗 Source: {image_url}"
        except Exception as e:
            logger.warning(f"[SAVE_IMAGE] Element screenshot failed: {e}")
        
        # Method 2: Download via page context
        try:
            async with wi.page.context.expect_download(timeout=3000) as download_info:
                await wi.page.goto(image_url)
            download = await download_info.value
            await download.save_as(filepath)
            file_size = os.path.getsize(filepath)
            return f"✅ Image saved successfully via browser download!\n📁 File: {filename}\n📂 Path: {filepath}\n📊 Size: {file_size:,} bytes\n🔗 Source: {image_url}"
        except Exception as e:
            logger.warning(f"[SAVE_IMAGE] Context download failed: {e}")
        
        # Method 3: Fallback to HTTP request with browser headers.
        # SSRF protection (2026-09 audit): the old bare ClientSession() here had NO
        # validation or pinning at all -- a page could point its "main image" at an
        # internal address and this fallback would happily fetch it. Same guard as
        # download_file: fail-closed pre-check + safe_session (pinned connector).
        try:
            from tools.ssrf_guard import ensure_url_not_ssrff, safe_session, SSRFError as _SSRFError
            try:
                ensure_url_not_ssrff(image_url)
            except Exception as e:  # includes SSRFError -> hard stop, no fallback path
                logger.error(f"SSRF validation failed in save_current_image, refusing to proceed (no fallback): {e!r}")
                return "Error: Access blocked for security."

            import aiohttp
            cookies = await wi.context.cookies()
            cookie_header = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
                'Referer': wi.page.url,
                'Cookie': cookie_header
            }
            
            try:
                async with safe_session(timeout=120) as session:
                    async with session.get(image_url, headers=headers) as response:
                        if response.status == 200:
                            content = await response.read()
                            with open(filepath, 'wb') as f:
                                f.write(content)
                            return f"✅ Image saved successfully!\n📁 File: {filename}\n📂 Path: {filepath}\n📊 Size: {len(content):,} bytes\n🔗 Source: {image_url}"
                        else:
                            return f"❌ Failed to save image: HTTP {response.status}"
            except _SSRFError as e:
                logger.error(f"SSRF protection triggered in save_current_image fallback: {e}")
                return "Error: Access blocked for security."
        except Exception as e:
            return f"❌ Failed to save image: {str(e)}"
        
    except Exception as e:
        logger.error(f"[SAVE_IMAGE] Failed: {e}")
        return f"❌ Failed to save image: {str(e)}"
