"""
WebSocket file/media handling.

Processes uploaded media files (images, text files) attached to messages.
Returns enhanced message text and image data for vision processing.

UPDATED (2026-08-24):
  - Per-file error isolation: one unreadable/oversized attachment no longer
    drops the other attachments from the same message.
  - The saved filename of EVERY attached image is listed explicitly in the
    enhanced prompt, so the model can point to it by name later (chat history
    keeps that text) even though the pixels travel separately as base64.
  - MIME type per file is reported instead of assuming jpeg for everything.
"""

import os
import logging
from typing import List, Dict, Tuple

from app.utils.common import (
    is_image_file,
    is_text_file,
    encode_image_to_base64,
    read_text_file,
    guard_path_inside_working_root,
)
from app.utils.common import get_working_root

logger = logging.getLogger("COOLEMS.WebSocket.Files")


# Map image extension -> MIME type for the prompt reference line.
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
}


def _mime_for(filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    return _IMAGE_MIME.get(ext, "application/octet-stream")


def _resolve_upload_path(upload_dir: str, file_url: str) -> str | None:
    """Map an attachment URL (``/files/<rel>``) to a real path INSIDE *upload_dir*.

    Returns None for anything that is not a plain working-root-relative path:
    absolute paths, drive letters, leading separators, UNC targets or ``..``
    traversal.  Rejected instead of raised, so one bad attachment cannot break
    the rest of the message (the per-file isolation contract above).

    FIX (2026-11-13): the old code did ``file_url.replace("/files/", "")`` and a
    bare ``os.path.join``.  That stripped the prefix but kept the traversal tail
    (``/files/../x`` -> ``../x``), and ``os.path.join`` DISCARDS *upload_dir*
    when the remainder is absolute - so any file on the machine could be read
    into the prompt.  Containment is now enforced by the shared guard.
    """
    rel = str(file_url).replace("\\", "/")
    if rel.startswith("/files/"):
        rel = rel[len("/files/"):]        # prefix only, once - never replace() mid-string
    rel = rel.strip()
    if not rel or rel.startswith("/") or os.path.splitdrive(rel)[0]:
        return None
    filepath = os.path.normpath(os.path.join(upload_dir, rel))
    try:
        guard_path_inside_working_root(filepath, upload_dir, label="Attachment path")
    except ValueError:
        return None
    return filepath


def process_media_files(
    user_msg: str,
    media_files: List[str],
    conv_id: str | None = None,
) -> Tuple[str, List[str], List[str], List[Dict]]:
    """
    Process media files attached to a user message.

    Args:
        user_msg: The original user message text.
        media_files: List of file URL/paths from the client.
        conv_id: Conversation id - when given, uploads are resolved against THIS
            workspace's own working root (per-workspace DB row) instead of whatever
            folder happens to be active in the UI right now (2026-10-09).

    Returns:
        Tuple of (enhanced_message, image_data, image_paths, file_contents)
        - enhanced_message: User message with saved filenames + context appended
        - image_data: List of base64-encoded image strings for vision API
          (one entry per attached image, in the same order as media_files)
        - image_paths: List of image file paths (/files/<savedname>) for reference
        - file_contents: List of dicts with filename/content for text files
    """
    # Per-workspace resolution (2026-10-09): the message's own workspace folder when a
    # conversation id is known - concurrent chats then never mix up upload locations.
    if conv_id:
        from app.utils.common import resolve_working_root_for_conv
        UPLOAD_DIR = resolve_working_root_for_conv(conv_id)
    else:
        UPLOAD_DIR = get_working_root()

    image_data: List[str] = []
    image_refs: List[Dict] = []   # {"filename", "path", "mime"} per successful image
    failed_images: List[str] = []
    file_contents: List[Dict] = []

    for file_url in media_files:
        filepath = _resolve_upload_path(UPLOAD_DIR, file_url)
        if filepath is None:
            logger.warning(f"Rejected attachment (not a working-root file): {file_url!r}")
            failed_images.append(os.path.basename(str(file_url).replace("\\", "/")))
            continue
        # Derived from the RESOLVED path, never from the client string, so no
        # attacker-controlled text reaches the prompt through the filename.
        filename = os.path.relpath(filepath, UPLOAD_DIR).replace(os.sep, "/")

        try:
            if not os.path.exists(filepath):
                logger.warning(f"Media file not found: {filepath}")
                failed_images.append(filename)
                continue

            if is_image_file(filename):
                b64_data = encode_image_to_base64(filepath)
                if b64_data:
                    image_data.append(b64_data)
                    ref_path = f"/files/{filename}"
                    image_refs.append({
                        "filename": filename,
                        "path": ref_path,
                        "mime": _mime_for(filename),
                    })
                    logger.info(f"Encoded image for current message: {filename}")
                else:
                    # encode_image_to_base64 returns None on oversize/read errors.
                    failed_images.append(filename)

            elif is_text_file(filename):
                content = read_text_file(filepath)
                file_contents.append({
                    "filename": filename,
                    "content": content,
                    "type": "text",
                })
        except Exception as e:
            # One bad attachment must never drop the rest of the message's media.
            logger.error(f"Failed to process attached file {filename}: {e}")
            failed_images.append(filename)

    # Build enhanced message
    enhanced_message = user_msg

    if image_refs:
        # Explicit saved-filename block (one line per image, multiple supported).
        # This text is what stays in chat history, so the model can point to a
        # specific attached image by its exact saved name on later turns.
        lines = [f"[{len(image_refs)} image(s) attached - pixels are included as base64 with this message:"]
        for ref in image_refs:
            lines.append(f"  - saved file: {ref['filename']} (mime: {ref['mime']}, path: {ref['path']})")
        if failed_images:
            lines.append(f"  - NOT available (unreadable/oversized): {', '.join(failed_images)}")
        lines.append("Use these exact saved filenames when referring to or re-loading the images later.]")
        enhanced_message = "\n".join(lines) + f"\n{enhanced_message}"
        logger.info(
            f"Message includes {len(image_refs)} image(s) for vision processing: "
            + ", ".join(r["filename"] for r in image_refs)
        )

    if file_contents:
        enhanced_message += "\n\n[Attached Files:\n"
        for fc in file_contents:
            enhanced_message += f"\n--- {fc['filename']} ---\n{fc['content']}\n"
        enhanced_message += "]"

    return enhanced_message, image_data, [r["path"] for r in image_refs], file_contents
