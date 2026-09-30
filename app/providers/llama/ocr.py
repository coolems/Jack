"""
OCR transcription for llama.cpp provider.

Uses multimodal vision models to extract text from images.

Uses shared async HTTP client pool from http_client module instead of sync
requests.post() — this unblocks the event loop and enables connection reuse.

(2026-08-26 DEDICATED OCR FALLBACK)
The primary attempt POSTs to the main llama-server (config.LLAMA_URL). When that
server is running a NON-vision model (no mmproj loaded), llama.cpp answers:

    HTTP 500 {"error": "image input is not supported - ... you may need to provide the mmproj"}

In that case — and only in that case — this module transparently ensures a small
DEDICATED OCR instance (profiles.json 'ocr_model' dir, e.g. GLM-OCR + its mmproj)
is running on its own side port (config.LLAMA_OCR_SERVER_PORT) and retries the
EXACT same payload there. The main chat server is never touched or restarted.

The request format below MUST stay byte-for-byte what the OCR model likes:
prompt exactly "Text Recognition:", image as data-URL base64, temperature 0.0,
stream false. Changing it breaks OCR model performance.
"""
import asyncio
import logging
from typing import List

import httpx

from config import OCR_TIMEOUT, OCR_MAX_TOKENS

from .http_client import get_async_client
from .vision_support import _looks_like_mmproj_error

logger = logging.getLogger("COOLEMS.Provider.Llama.OCR")


async def ocr_transcribe(
    api_url: str,
    image_base64: str,
    ocr_models: List[str],
) -> str:
    """
    Transcribe text from an image using llama.cpp vision models.

    NOTE: The prompt MUST remain exactly "Text Recognition:"
    Changing it breaks OCR model performance.

    Flow:
      1. Primary attempt against `api_url` (the main chat server). Works when the
         loaded model is vision-capable (mmproj present at launch).
      2. If the failure is specifically "image input not supported" (loaded model
         has no mmproj), ensure the dedicated OCR instance is up and retry there
         with the identical payload.
      3. Any other error surfaces as a clean, actionable message (no raw JSON dump).
    """
    last_error = None

    # ---- Attempt 1: primary server (exact GLM-OCR call format) ----
    result_or_err = await _ocr_post(api_url, image_base64, ocr_models)

    if isinstance(result_or_err, str) and not result_or_err.startswith("ERROR"):
        return result_or_err  # success on the main server — done

    last_error = result_or_err

    # ---- Attempt 2: dedicated OCR instance (only for the mmproj/500 condition) ----
    if _looks_like_mmproj_error(str(last_error)):
        logger.info("[OCR] Main model has no vision (mmproj missing) — engaging dedicated OCR server fallback")
        try:
            from . import ocr_server_manager
            ocr_url = await ocr_server_manager.ensure_ocr_server()
        except Exception as e:
            logger.error(f"[OCR] Dedicated OCR server manager failed: {e}", exc_info=True)
            ocr_url = None

        if ocr_url and ocr_url.rstrip("/") != api_url.rstrip("/"):
            result_or_err2 = await _ocr_post(ocr_url, image_base64, ocr_models)
            if isinstance(result_or_err2, str) and not result_or_err2.startswith("ERROR"):
                # (2026-08-27) Optional post-use unload: free the dedicated instance’s VRAM
                # per config.LLAMA_OCR_UNLOAD_AFTER_USE. Never blocks the event loop and a
                # failed unload must not break an already-successful transcription.
                try:
                    from config import LLAMA_OCR_UNLOAD_AFTER_USE
                    if LLAMA_OCR_UNLOAD_AFTER_USE:
                        from . import ocr_server_manager
                        await asyncio.to_thread(ocr_server_manager.unload_ocr_server)
                except Exception as e:
                    logger.warning(f"[OCR] Post-use OCR unload failed (result unaffected): {e}")
                return result_or_err2  # success on the dedicated instance — done
            last_error = (f"{last_error} | Dedicated OCR server: {result_or_err2}")

        from .vision_support import build_vision_error_message
        clean_msg, _mmproj_path = build_vision_error_message(ocr_models[0] if ocr_models else "")
        return f"ERROR: All OCR models failed. Last error: {last_error} — {clean_msg}"

    # ---- Non-vision failure (network down, other 5xx) — keep it clean & actionable ----
    from .vision_support import build_vision_error_message
    clean_msg, _mmproj_path = build_vision_error_message(ocr_models[0] if ocr_models else "")
    return f"ERROR: All OCR models failed. Last error: {last_error} — {clean_msg}"


async def _ocr_post(api_url: str, image_base64: str, ocr_models: List[str]):
    """POST the exact OCR payload to one llama-server base URL.

    Returns the extracted text on success, or an "ERROR: ..." string on failure.
    The payload format is fixed by the model — do not modify it.
    """
    # NOTE: The prompt MUST remain exactly "Text Recognition:"
    # Changing it breaks OCR model performance.
    OCR_PROMPT = "Text Recognition:"

    last_error = None
    http_client = await get_async_client()

    for model in ocr_models:
        try:
            logger.info(f"[OCR] Trying llama.cpp model: {model} @ {api_url}")
            ocr_payload = {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                    ]
                }],
                "stream": False,
                "temperature": 0.0,
                "max_tokens": OCR_MAX_TOKENS,
            }

            # Use shared async client pool — no blocking sync requests
            response = await http_client.post(
                f"{api_url}/v1/chat/completions",
                json=ocr_payload,
                timeout=OCR_TIMEOUT,
            )

            if response.status_code == 200:
                result = response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if result:
                    logger.info(f"[OCR] SUCCESS with {model} @ {api_url}: Extracted {len(result)} characters")
                    return result
                last_error = "No text in response"
                continue
            else:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(f"[OCR] Model {model} @ {api_url} failed: {last_error}")
                continue
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[OCR] Model {model} @ {api_url} error: {e}")
            continue

    return f"ERROR: All OCR models failed. Last error: {last_error}"
