"""File I/O helpers — type detection, size limits, read/write utilities."""

import base64
import io
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger("COOLEMS.Tools")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FILE_MAX_SIZE_BYTES = 100 * 1024 * 1024   # 100MB
FILE_MAX_TEXT_CONTENT_BYTES = 500 * 1024  # 500KB
FILE_MAX_WRITE_BYTES = 10 * 1024 * 1024   # 10MB

# Vision attachment budget (2026-09-10): attached images are downscaled to JPEG so
# their base64 payload stays under this cap. ~500KB of pixels ~= 670K chars base64,
# i.e. a few K vision tokens - small enough that several images per turn fit the
# context window while leaving room for the model's answer.
VISION_IMAGE_MAX_BYTES = 500 * 1024        # target max JPEG size after downscaling (~500 KB)
VISION_IMAGE_MAX_BASE64_CHARS = 700_000   # ~525KB raw -> trigger to compress

# Backward-compatible aliases (some code imports these names)
MAX_FILE_SIZE = FILE_MAX_SIZE_BYTES
MAX_TEXT_CONTENT_SIZE = FILE_MAX_TEXT_CONTENT_BYTES
MAX_WRITE_SIZE = FILE_MAX_WRITE_BYTES


TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".log",
    ".py", ".js", ".html", ".css", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".conf",
    ".csv", ".tsv", ".sh", ".bat", ".ps1", ".sql",
    ".cpp", ".c", ".h", ".hpp", ".java", ".kt", ".rs", ".go", ".rb", ".php", ".pl", ".r", ".m", ".swift", ".scala",
}

EXCLUDED_FOLDERS = {
    "__pycache__", "agent_prompt", "_old", "dna",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}


# ---------------------------------------------------------------------------
# File type helpers
# ---------------------------------------------------------------------------


def is_text_file(filename: str) -> bool:
    """Return True when *filename* has a known text extension."""
    ext = os.path.splitext(filename.lower())[1]
    return ext in TEXT_EXTENSIONS


def is_image_file(filename: str) -> bool:
    """Return True when *filename* has a known image extension."""
    ext = os.path.splitext(filename.lower())[1]
    return ext in IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


def _compress_image_to_jpeg_bytes(image_bytes: bytes, max_bytes: int = VISION_IMAGE_MAX_BYTES):
    """Compress an in-memory image to JPEG so its payload stays <= ~max_bytes.

    Strategy (2026-09-10 multi-image fix - see plan_20260910_1835.md):
      1. Try the original size at decreasing quality steps until it fits.
      2. If even the smallest quality is too big, downscale (Lanczos) in 0.7x
         steps and retry - resolution costs more tokens than mild JPEG artifacts.

    Returns JPEG bytes on success, or None when Pillow is unavailable / the image
    cannot be decoded (caller then falls back to encoding the original file).

    Note: GIF/WebP/TIFF are flattened to their FIRST frame; alpha is composited
    onto white so screenshots keep readable text.
    """
    try:
        from PIL import Image, ImageOps  # lazy: Pillow is optional at runtime
    except ImportError:
        logger.warning("Pillow not installed - cannot downscale image (add 'pillow' to CLIENT requirements)")
        return None

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)          # respect camera orientation
        if img.mode in ("RGBA", "LA", "P"):         # flatten transparency onto white
            rgba = img.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        scale = 1.0
        for _pass in range(8):                      # bounded: at most 8 downscale passes
            w, h = img.size
            if scale < 1.0:
                new_size = (max(64, int(w * scale)), max(64, int(h * scale)))
                img = img.resize(new_size, Image.LANCZOS)
            for quality in (85, 75, 65, 55, 45):
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                data = buf.getvalue()
                if len(data) <= max_bytes:
                    return data
            scale *= 0.7                            # still too big -> downscale more
        return None                                 # gave up (should be practically impossible)
    except Exception as e:
        logger.error("Failed to compress image to JPEG: %s", e)
        return None


def encode_image_to_base64(filepath: str) -> Optional[str]:
    """Read an image file and return its base64 payload for vision processing.

    BIG-IMAGE FIX (2026-09-10): when the raw file would produce a base64 string
    larger than VISION_IMAGE_MAX_BASE64_CHARS (~500KB of pixels), the image is
    re-encoded as a downscaled JPEG capped at ~VISION_IMAGE_MAX_BYTES. A 7MB PNG
    screenshot used to ride into the prompt as 9.3M base64 chars, which alone
    nearly filled the whole context window and starved the output (the clamp hit
    its 1024-token floor and every answer was cut off mid-sentence).

    Returns ``None`` when the file exceeds :data:`MAX_FILE_SIZE` or an error occurs.
    """
    try:
        file_size = os.path.getsize(filepath)
        if file_size > MAX_FILE_SIZE:
            logger.warning("Image too large: %s (%d bytes)", filepath, file_size)
            return None

        with open(filepath, "rb") as f:
            image_bytes = f.read()

        # base64 grows the payload by ~4/3; only compress when it would exceed the cap.
        if len(image_bytes) * 4 // 3 > VISION_IMAGE_MAX_BASE64_CHARS:
            logger.info(
                "Image %s is big (%d bytes raw, ~%d chars base64) - downscaling to JPEG <= %d KB",
                os.path.basename(filepath), file_size, len(image_bytes) * 4 // 3,
                VISION_IMAGE_MAX_BYTES // 1024,
            )
            compressed = _compress_image_to_jpeg_bytes(image_bytes)
            if compressed is not None:
                image_bytes = compressed

        b64_data = base64.b64encode(image_bytes).decode("utf-8")
        logger.info(
            "Encoded image %s: %d chars base64", os.path.basename(filepath), len(b64_data)
        )
        return b64_data
    except Exception as e:
        logger.error("Failed to encode image %s: %s", filepath, e)
        return None


def read_text_file(filepath: str, max_size: int = MAX_TEXT_CONTENT_SIZE) -> str:
    """Read text file with encoding detection.

    Tries multiple encodings (UTF-8 first), falls back to binary decode
    with ``errors='ignore'`` when all named encodings fail.

    Raises
    ------
    Exception
        Re-raises any exception that occurs during reading.
    """
    try:
        size = os.path.getsize(filepath)
        if size > max_size:
            return f"File too large ({size} bytes), truncated to {max_size}"

        encodings = ["utf-8", "utf-16", "ascii", "latin-1", "cp1252"]
        for enc in encodings:
            try:
                with open(filepath, "r", encoding=enc) as f:
                    content = f.read(max_size)
                return "".join(c for c in content if ord(c) >= 32 or c in "\n\r\t")
            except (UnicodeDecodeError, UnicodeError):
                continue

        # Fallback: read as binary and decode with ignore
        with open(filepath, "rb") as f:
            content = f.read(max_size)
        return content.decode("utf-8", errors="ignore")
    except Exception as e:
        logger.error("Failed to read file %s: %s", filepath, e)
        raise


def get_file_info(filepath: str, filename: str) -> Dict:
    """Get file metadata and optional content.

    Returns a dict with keys: ``path``, ``name``, ``is_text``, ``content``, ``size``.
    For text files the full content is included; for binary files only metadata.
    """
    info = {"path": filepath, "name": filename, "is_text": False, "content": None, "size": 0}
    try:
        info["size"] = os.path.getsize(filepath)
        if is_text_file(filename):
            info["is_text"] = True
            info["content"] = read_text_file(filepath)
    except Exception as e:
        logger.error("Failed to get file info %s: %s", filepath, e)
    return info
