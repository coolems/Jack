"""Vision (multimodal input) support detection + typed error for llama.cpp.

WHY THIS EXISTS
When llama-server is started WITHOUT an mmproj file it accepts image input and answers
with HTTP 500 - an opaque failure the user reads as "the model is broken". This module
checks whether the loaded model actually has a projector and raises VisionNotAvailableError
with the actionable message instead, so the turn fails with a reason the user can act on.
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger("COOLEMS.Provider.Llama.Vision")


class VisionNotAvailableError(Exception):
    """Raised when a request carries image input but the loaded model has no mmproj.

    This is an EXPECTED user-facing condition (user sent an image to a non-vision
    setup), not a server fault — callers log it as a single clean warning line,
    never with a traceback.

    Attributes:
        error_type:  Stable tag for the WS error frame ("vision_not_available").
        model:       The requested/loaded model name (as received in the payload).
        mmproj_path: Full path to a compatible projector if one was found on disk,
                     else None. Lets the UI tell the user exactly what to do.
    """

    error_type = "vision_not_available"

    def __init__(self, message: str, model: Optional[str] = None,
                 mmproj_path: Optional[str] = None):
        super().__init__(message)
        self.model = model
        self.mmproj_path = mmproj_path


def _looks_like_mmproj_error(err_text: str) -> bool:
    """True when a llama.cpp error body is the 'image input not supported' case."""
    low = (err_text or "").lower()
    return "image input is not supported" in low or "you may need to provide the mmproj" in low


def check_model_vision(model_filename: str) -> Dict:
    """Report whether a model has vision, or needs an mmproj next to it.

      1. Resolve the requested model filename to a real path (may be None - info is still
         useful in that case).
      2. Search for a compatible multimodal projector with the same logic as
         model_utils._find_dedicated_mmproj.

    Returns a dict with bool "mmproj_found" and "mmproj_path" ("" when none).
    Never raises - any lookup problem degrades to mmproj_found=False.
    """
    from .path_utils import _resolve_model_path
    from .model_discovery import _find_mmproj_file

    resolved: Optional[str] = None
    try:
        resolved = _resolve_model_path(model_filename)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"[VISION] Could not resolve model path for {model_filename!r}: {e}")
        resolved = None

    mmproj = ""
    if resolved:
        try:
            mmproj = _find_mmproj_file(resolved) or ""
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"[VISION] mmproj lookup failed for {resolved}: {e}")
            mmproj = ""

    return {
        "model": model_filename,
        "resolved_path": resolved,
        "mmproj_found": bool(mmproj),
        "mmproj_path": mmproj,
    }


def build_vision_error_message(model_filename: str) -> tuple[str, Optional[str]]:
    """Build a clean, apologetic, ACTIONABLE message for the UI.

    Returns (message, mmproj_path_or_None). The two wordings differ by whether a
    compatible projector exists on disk (so we can point at it) or not (model has
    no vision support available to us right now).
    """
    info = check_model_vision(model_filename)

    if info["mmproj_found"]:
        message = (
            "I'm sorry, but vision is not available for this model right now. "
            "The AI server was started without a multimodal projector (mmproj), so I can't see images. "
            f"A compatible projector was found at: {info['mmproj_path']}. "
            "Restart the server with that mmproj loaded and image input will work."
        )
    else:
        message = (
            "I'm sorry, but vision is not available for this model right now. "
            "It has no multimodal projector (mmproj) in its directory, so it cannot process images. "
            "To view images, either switch to a vision-capable model or place a matching mmproj file "
            "in the same folder as the model and restart the server."
        )

    return message, (info["mmproj_path"] or None)


def make_vision_error(model_filename: str) -> VisionNotAvailableError:
    """Convenience: build + raise-ready VisionNotAvailableError for a model."""
    message, mmproj_path = build_vision_error_message(model_filename)
    return VisionNotAvailableError(message, model=model_filename, mmproj_path=mmproj_path)
