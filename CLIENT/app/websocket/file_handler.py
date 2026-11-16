"""Upload/attachment handling for WebSocket messages.

Processes media files attached to a message and returns the enhanced message text plus
image data for vision processing.

  - Per-file error isolation: one unreadable or oversized attachment does not drop the
    others from the same message.
  - The saved filename of EVERY attached image is listed in the enhanced prompt, so the
    model can name it later (chat history keeps that text) while pixels travel as base64.
  - MIME type is detected per file instead of assuming jpeg.
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

    Rejects absolute paths, drive letters, leading separators, UNC targets and ``..``
    traversal. Rejected rather than raised, so one bad attachment cannot break the rest of
    the message (the per-file isolation contract above). os.path.join would DISCARD
    *upload_dir* when the remainder is absolute, so containment is enforced by the shared
    guard before the join.
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
    """Process media files attached to a user message.

    Args:
        user_msg: the original message text.
        media_files: file URL/paths from the client.
        conv_id: conversation id - when given, uploads resolve against THIS workspace's own
            working root (its DB row) instead of the folder currently open in the UI.
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
