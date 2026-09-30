"""Transcribe image function - extracts text from images using OCR model

UPDATED (2026-08-26):
  - Optional `image_base64` parameter: when the caller already has the raw pixels
    (e.g. an image just uploaded in this chat turn), they are used DIRECTLY -- no
    disk read needed at all.
  - Per-turn attachment bridge: if `filename` matches an image attached to the
    CURRENT chat turn, its in-memory base64 is used directly (the exact same bytes
    that ride on the user message for vision) instead of re-reading from disk.
  - Auto-save: when transcribing pixels that do not exist as a file yet (direct
    base64 with no saved name), the image is saved into working_root under a clear
    name and that name is reported in the result, so it can be referenced later.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "transcribe_image",
        "description": "Extract text from an image using OCR. Returns the raw extracted text from the image. Image file must be inside working_root directory, OR provide its pixels directly via image_base64.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Filename of the image to transcribe. Can include folder path relative to working_root (e.g. 'my_image.png' or 'subfolder/my_image.png'). File must exist inside working_root."
                },
                "image_base64": {
                    "type": "string",
                    "description": "Optional: raw base64-encoded image bytes (no data-URL prefix). When provided, these pixels are transcribed directly and no file is read from disk. If the image has no saved filename yet it is auto-saved into working_root and its name is reported in the result."
                }
            },
            "required": []
        }
    }
}

import logging

from config import OCR_TIMEOUT
import base64
from ..utils import find_file, is_image_file

logger = logging.getLogger("COOLEMS.Tools.Vision")


def _looks_like_base64(data: str) -> bool:
    """Cheap sanity check that a string is plausibly raw base64 image data."""
    if len(data) < 100:
        return False
    import re

    sample = data[:512]
    # '=' padding can only appear at the very end of the payload, so a prefix
    # check on the alphabet is safe for long payloads.
    return bool(re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", sample))


def _auto_save_name() -> str:
    """Build a unique auto-save filename for pixels that have no saved name yet."""
    import os
    from datetime import datetime

    root = get_working_root_safe()
    base_name = "transcribed_image_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".png"
    candidate = os.path.join(root, base_name)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(root, f"{base_name[:-4]}_{counter}.png")
        counter += 1
    return os.path.basename(candidate)


def get_working_root_safe() -> str:
    """Working root with a friendly error instead of an uncaught RuntimeError."""
    from ..utils import get_working_root

    try:
        return get_working_root()
    except Exception as e:
        raise RuntimeError(str(e))


def transcribe_image(filename: str = "", image_base64: str = "") -> str:
    """
    Transcribe/extract text from an image using OCR.
    Uses the active provider (Ollama or llama.cpp) from ProviderManager.
    Returns extracted text.

    Args:
        filename: Image file name. Can include relative folder path (e.g. 'screenshot.png' or 'images/screenshot.png').
                  File is searched for inside working_root using find_file().
        image_base64: Optional raw base64-encoded image bytes (no data-URL prefix).
                      When provided, these pixels are used directly -- no disk read.
    """
    import os

    filename = (filename or "").strip()
    image_base64 = (image_base64 or "").strip()

    if not filename and not image_base64:
        return "Error: Provide either 'filename' (an image inside working_root) or 'image_base64' (raw base64 pixels)."

    source_note = ""  # prefix added to the result explaining where the pixels came from

    try:
        if image_base64:
            # ---- DIRECT PIXELS PATH (2026-08-26): caller already has the bytes ----
            if not _looks_like_base64(image_base64):
                return "Error: 'image_base64' does not look like valid base64 data."

            if filename:
                # Pixels + a name: verify the file really exists so we can report it.
                try:
                    filepath = find_file(filename)
                except Exception as e:
                    return f"Error: Could not search for '{filename}': {e}"
                if not filepath or not is_image_file(filepath):
                    return f"Error: Image file not found (direct pixels were provided for an unknown name): {filename}"

            else:
                # Pixels without a saved name -> auto-save so the image has a name later.
                try:
                    root = get_working_root_safe()
                except Exception as e:
                    return f"Error: Cannot determine working_root to save the image: {e}"
                saved_filename = _auto_save_name()
                target_path = os.path.join(root, saved_filename)
                with open(target_path, "wb") as f:
                    f.write(base64.b64decode(image_base64))
                source_note = (f"[Image auto-saved to working_root as '{saved_filename}' - use this exact name to refer to it later.]\n\n")
                logger.info(f"[OCR] Auto-saved direct-base64 image as {saved_filename}")

        else:
            # ---- FILE PATH: resolve the image inside working_root ----
            try:
                filepath = find_file(filename)
            except Exception as e:
                return f"Error: Could not search for '{filename}': {e}"

            if not filepath:
                # (2026-08-26 per-turn bridge): the file may be an image attached to
                # THIS chat turn -- its pixels are already in memory, use them directly.
                try:
                    from ..utils import get_current_turn_attachments
                    attachments = get_current_turn_attachments()
                    key = filename.split("/")[-1]
                    if key in attachments:
                        logger.info(f"[OCR] Using in-memory pixels of turn-attached image: {key}")
                        source_note = (f"[Used the base64 pixels attached to this message directly "
                                       f"(saved file: {key}).]\n\n")
                        image_base64 = attachments[key]
                    else:
                        return f"Error: Image file not found: {filename}"
                except Exception as e:
                    logger.debug(f"[OCR] Turn-attachment lookup unavailable ({e}) - falling back to disk-only error")
                    return f"Error: Image file not found: {filename}"

            if not image_base64:
                # Normal case: read the bytes from disk.
                if not is_image_file(filepath):
                    return f"Error: File is not an image: {filename}"
                try:
                    with open(filepath, 'rb') as f:
                        image_base64 = base64.b64encode(f.read()).decode('utf-8')
                except Exception as e:
                    return f"Error: Could not read image file: {str(e)}"

        # ---- Run OCR through the active provider (SERVER-side delegation) ----
        try:
            from app.provider_manager import ProviderManager
        except ImportError:
            return "Error: ProviderManager not available. Provider not initialized."

        provider = ProviderManager.get_provider()
        if provider is None:
            return "Error: No active provider configured."

        ocr_models = ProviderManager.get_ocr_models()
        if not ocr_models:
            return "Error: No OCR models configured."

        logger.info(f"[OCR] Transcribing image ({'direct/turn pixels' if source_note else 'from file'}): {filename or '<direct pixels>'}")

        # Call provider's async OCR method.
        # (2026-09 threadless refactor): the second executor layer is GONE. In production this tool
        # body runs inside asyncio.to_thread() from RemoteToolOrchestrator.execute_tool - i.e. in a
        # worker thread with NO running event loop - so ONE plain asyncio.run() around the await is
        # the honest bridge (no ThreadPoolExecutor wrapping it). The OCR_TIMEOUT budget is enforced
        # INSIDE that loop via wait_for, so a hung SERVER-side transcription is cancelled cleanly
        # instead of leaking a blocked worker thread forever.
        import asyncio

        async def _do_ocr():
            return await provider.ocr_transcribe(image_base64, ocr_models)

        try:
            result = asyncio.run(asyncio.wait_for(_do_ocr(), timeout=OCR_TIMEOUT))
        except (asyncio.TimeoutError, TimeoutError):
            logger.error(f"[OCR] OCR timed out after {OCR_TIMEOUT}s")
            return f"Error: OCR transcription timed out after {OCR_TIMEOUT} seconds."
        except Exception as e2:
            logger.error(f"[OCR] Async execution failed: {e2}")
            return f"Error: OCR execution failed: {str(e2)}"

        if isinstance(result, str) and result.startswith("ERROR"):
            # Surface the provider error cleanly (keep it for the model to react to).
            return source_note + result if source_note else result
        return source_note + (result or "")

    except Exception as e:
        logger.error(f"[OCR] Transcription failed: {e}", exc_info=True)
        return f"Error: OCR transcription failed: {str(e)}"