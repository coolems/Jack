"""Find and Move to Target - find_and_move_to_target()

Purpose: bridge "I want to click X" -> "where exactly is X on this screenshot?"

The Problem:
- You want to click on "the search button" but you need X,Y coordinates
- The vision model can see the image but returns text, not coordinates
- You need a way to bridge "what I see" to "where to click"

What this tool ACTUALLY does (it is an annotation + prompt helper, NOT a locator):
1. Validates the screenshot path inside working_root (path_guard)
2. Reads the image dimensions
3. Stamps a black text banner ("TARGET: <description>" + size + instruction line)
   across the top of the screenshot and saves it as annotated_<name> in working_root
4. Returns the annotated image BOTH as a file path AND as image_base64 (so the AI
   sees it automatically in the tool result - no separate view_image() needed),
   plus a ready-made vision_prompt asking for [left, top, right, bottom]

It does NOT call any vision model and does NOT return coordinates or confidence.
The AI reads the returned image, locates the target itself, then moves the mouse
with set_pos() to its own chosen coordinates and clicks.

IMPORTANT: Returns image_base64 so the AI automatically sees the verification
image as part of the tool result - no separate view_image() call needed.
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "find_and_move_to_target",
        "description": "Prepares a browser screenshot for precise manual targeting. Stamps the target description as a text banner on the screenshot and returns the annotated image (path + base64, visible in the result) plus a vision prompt asking for [left, top, right, bottom] coordinates. It does NOT call a vision model or compute coordinates itself: view the returned image, locate the element yourself, then move the mouse with set_pos() and click it. Use whenever you need to target an element on screen.",
        "parameters": {
            "type": "object",
            "properties": {
                "screenshot_path": {
                    "type": "string",
                    "description": "Path to the screenshot image inside working_root, e.g. from browser_screenshot()."
                },
                "target_description": {
                    "type": "string",
                    "description": "What element to target (e.g., 'search button', 'first property card'). Stamped on the annotated image."
                },
                "image_width": {
                    "type": "number",
                    "description": "Optional width override in pixels; defaults to the actual image width read from the file."
                },
                "image_height": {
                    "type": "number",
                    "description": "Optional height override in pixels; defaults to the actual image height read from the file."
                }
            },
            "required": ["screenshot_path", "target_description"]
        }
    }
}

import logging
import os
import re
import base64

from ..utils import get_working_root, find_file, validate_file_access

logger = logging.getLogger("COOLEMS.Tools.VisualBrowserNavigation")


def _resolve_screenshot_path(path_str: str) -> str:
    """Resolve a screenshot path safely inside working_root.

    Tries ``find_file()`` first (which validates containment via secure_find_file),
    then falls back to direct validation via the canonical ``validate_file_access()``
    from ``tools.path_guard`` — single source of truth for all path safety checks.
    Raises ``ValueError`` / ``FileNotFoundError`` on any escape attempt.
    """
    # --- Fast path: find_file already secures the lookup -------------------
    resolved = find_file(path_str)
    if resolved and os.path.isfile(resolved):
        return resolved

    # --- Fallback: direct absolute path via canonical path_guard -----------
    return validate_file_access(
        path_str, get_working_root(), label="Screenshot"
    )


def _encode_image_to_base64(filepath):
    """Encode an image file to base64 string."""
    try:
        with open(filepath, 'rb') as f:
            image_bytes = f.read()
        return base64.b64encode(image_bytes).decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to encode image {filepath}: {e}")
        return None

def find_and_move_to_target(screenshot_path, target_description, image_width=None, image_height=None):
    """
    Prepare a screenshot for manual targeting of an element.

    This tool is the first half of "I want to click X" -> "move mouse to Y,Z":
    it gets you a clearly-labeled image to look at. It does NOT locate anything by
    itself - no vision model call, no bounding box drawing, no coordinates returned.

    Workflow:
    1. Validates the screenshot path inside working_root (path_guard)
    2. Loads the image and reads its real dimensions
    3. Stamps a black text banner ("TARGET: <description>" + size + instruction)
       on the screenshot, saved as annotated_<name> in working_root
    4. Returns the annotated image path AND base64 (auto-visible in tool result),
       plus a vision_prompt you can use to ask for [left, top, right, bottom]

    IMPORTANT: Returns image_base64 so the AI automatically sees the screenshot
    as part of the tool result - no separate view_image() call needed. After
    reviewing it, YOU determine the coordinates and act with set_pos()/click_left().

    SECURITY: All screenshot paths are validated inside working_root before access.

    Args:
        screenshot_path: Path to the screenshot to annotate (validated in working_root)
        target_description: What to find (e.g., "search button", "first property card")
        image_width: Optional width override in pixels (defaults to actual image size)
        image_height: Optional height override in pixels (defaults to actual image size)

    Returns:
        dict with:
        - success: bool
        - message: Human-readable status
        - image_path: Validated path of the original screenshot
        - image_width, image_height: Effective dimensions used
        - target_description: Echoed input
        - annotated_image: Path to the banner-stamped copy in working_root
        - image_base64: Base64-encoded annotated image (auto-visible to the AI)
        - next_action: What to do after reviewing the image
        - vision_prompt: Ready-made prompt asking for [left, top, right, bottom]

        On failure: success=False plus None/0.0 placeholders (target_box, center_x,
        center_y, annotated_image, confidence) kept for schema stability - they are
        always None/0.0 and must not be used.
    """
    try:
        # FIX (2026-08-23): lazy Pillow import -- a top-level `from PIL import ...`
        # made this tool silently UNAVAILABLE at compile time in venvs without
        # Pillow. Now a missing dependency produces a clean, honest tool result.
        try:
            from PIL import Image, ImageDraw  # noqa: F401 (used below + helpers)
        except ImportError as e:
            return {
                "success": False,
                "message": f"Pillow not installed ({e}). Run: pip install Pillow",
                "target_box": None,
                "center_x": None,
                "center_y": None,
                "annotated_image": None,
                "confidence": 0.0
            }

        # --- SECURITY: validate screenshot path inside working_root ---------
        safe_path = _resolve_screenshot_path(screenshot_path)

        # Load image to get dimensions
        img = Image.open(safe_path)
        actual_width, actual_height = img.size

        if image_width is None:
            image_width = actual_width
        if image_height is None:
            image_height = actual_height

        # Draw annotation on the screenshot
        annotated_result = _draw_annotation_on_screenshot(
            safe_path, target_description, actual_width, actual_height
        )

        # Encode the annotated image to base64 so AI sees it automatically
        image_base64 = _encode_image_to_base64(annotated_result["annotated_image"])

        return {
            "success": True,
            "message": f"Screenshot loaded ({image_width}x{image_height}). "
                      f"Target '{target_description}' marked on image. "
                      f"Review the image to locate '{target_description}' yourself and note its pixel position. ",
            "image_path": safe_path,
            "image_width": image_width,
            "image_height": image_height,
            "target_description": target_description,
            "annotated_image": annotated_result["annotated_image"],
            "image_base64": image_base64,
            "next_action": "Review the returned image, locate the target yourself, then move the mouse with set_pos() and click",
            "vision_prompt": f"Look at this screenshot and find: '{target_description}'. "
                           f"Return ONLY the bounding box coordinates as [left, top, right, bottom] "
                           f"in pixels. The image is {image_width}x{image_height}. "
                           f"Also return confidence level 0-100."
        }

    except (ValueError, FileNotFoundError) as e:
        # Security / existence errors -- surface directly
        return {
            "success": False,
            "message": str(e),
            "target_box": None,
            "center_x": None,
            "center_y": None,
            "annotated_image": None,
            "confidence": 0.0
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error loading screenshot: {str(e)}",
            "target_box": None,
            "center_x": None,
            "center_y": None,
            "annotated_image": None,
            "confidence": 0.0
        }

def _draw_annotation_on_screenshot(screenshot_path, target_description, image_width, image_height):
    """
    Stamp an annotation banner on the screenshot marking the intended target.

    Draws a black banner (50px) across the top with "TARGET: <description>" (yellow),
    the image size (white), and an instruction line asking for [left, top, right, bottom]
    (cyan). Saves as annotated_<name> in working_root. This is the verification image
    that the AI will see via the returned image_base64.

    Args:
        screenshot_path: Path to the original screenshot (already validated)
        target_description: What to find
        image_width: Image width
        image_height: Image height

    Returns:
        dict with annotated_image path
    """
    try:
        from PIL import Image, ImageDraw  # lazy -- see find_and_move_to_target note (2026-08-23)
        img = Image.open(screenshot_path)
        draw = ImageDraw.Draw(img)

        # Add annotation banner at top
        banner_height = 50
        draw.rectangle([0, 0, img.width, banner_height], fill="black")
        draw.text((10, 5), f"TARGET: {target_description}", fill="yellow")
        draw.text((10, 25), f"Size: {img.width}x{img.height}", fill="white")
        draw.text((10, 40), "Find this element and return [left, top, right, bottom]", fill="cyan")

        # Save annotated image
        working_dir = get_working_root()
        os.makedirs(working_dir, exist_ok=True)
        annotated_filename = f"annotated_{os.path.basename(screenshot_path)}"
        annotated_path = os.path.join(working_dir, annotated_filename)
        img.save(annotated_path)

        return {
            "annotated_image": annotated_path,
            "message": f"Annotation drawn on screenshot. Saved to {annotated_path}"
        }

    except Exception as e:
        return {
            "annotated_image": screenshot_path,
            "message": f"Annotation error: {str(e)}. Returning original screenshot."
        }

def _parse_coordinates_from_vision(vision_response):
    """
    [UNUSED HELPER - not called by any code path] Extract bounding box coordinates
    from a vision model text response.

    Extract bounding box coordinates from vision model response.

    Vision model should return something like:
    "[150, 200, 350, 280]" or "left: 150, top: 200, right: 350, bottom: 280"

    Args:
        vision_response: Text response from vision model

    Returns:
        dict with:
        - target_box: [left, top, right, bottom] or None
        - confidence: 0.0-1.0 or None
        - raw_response: Original vision response
    """
    try:
        # Try to find bracket notation [left, top, right, bottom]
        bracket_match = re.search(r'\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]', vision_response)
        if bracket_match:
            left, top, right, bottom = [int(x) for x in bracket_match.groups()]
            return {
                "target_box": [left, top, right, bottom],
                "confidence": None,  # Will be extracted separately
                "raw_response": vision_response
            }

        # Try key-value notation
        left_match = re.search(r'left[:\s]*(\d+)', vision_response, re.IGNORECASE)
        top_match = re.search(r'top[:\s]*(\d+)', vision_response, re.IGNORECASE)
        right_match = re.search(r'right[:\s]*(\d+)', vision_response, re.IGNORECASE)
        bottom_match = re.search(r'bottom[:\s]*(\d+)', vision_response, re.IGNORECASE)

        if all([left_match, top_match, right_match, bottom_match]):
            left = int(left_match.group(1))
            top = int(top_match.group(1))
            right = int(right_match.group(1))
            bottom = int(bottom_match.group(1))
            return {
                "target_box": [left, top, right, bottom],
                "confidence": None,
                "raw_response": vision_response
            }

        # Try to extract confidence
        confidence_match = re.search(r'confidence[:\s]*(\d+)', vision_response, re.IGNORECASE)
        confidence = None
        if confidence_match:
            confidence = int(confidence_match.group(1)) / 100.0

        return {
            "target_box": None,
            "confidence": confidence,
            "raw_response": vision_response,
            "error": "Could not parse coordinates from vision response"
        }

    except Exception as e:
        return {
            "target_box": None,
            "confidence": None,
            "raw_response": vision_response,
            "error": f"Parse error: {str(e)}"
        }

def _draw_annotated_box(screenshot_path, target_box, target_description, confidence=None):
    """
    [UNUSED HELPER - not called by any code path] Draw a bounding box on a screenshot.

    Draws a semi-transparent green fill + 4px blue border around target_box with a
    red crosshair at the center and labels (description/confidence/center coords);
    saves annotated_<name> in working_root and returns its base64 encoding.

    Visual elements:
    - Semi-transparent GREEN fill (so you can see what is underneath)
    - Thick BLUE border (4px) for clear visibility
    - RED crosshair at center point
    - Label with target description and confidence

    Args:
        screenshot_path: Path to the original screenshot (already validated)
        target_box: [left, top, right, bottom] bounding box
        target_description: What was targeted
        confidence: 0.0-1.0 confidence level

    Returns:
        dict with:
        - annotated_image: Path to annotated image
        - center_x, center_y: Calculated center coordinates
        - box_info: Box metadata
        - image_base64: Base64-encoded annotated image
    """
    try:
        from PIL import Image, ImageDraw  # lazy -- see find_and_move_to_target note (2026-08-23)
        img = Image.open(screenshot_path)
        draw = ImageDraw.Draw(img)

        left, top, right, bottom = target_box

        # Draw semi-transparent green overlay
        # Create overlay
        overlay = Image.new('RGBA', img.size, (0, 255, 0, 60))
        overlay_img = Image.new('RGBA', img.size, (0, 0, 0, 0))

        # Draw green rectangle on overlay
        overlay_draw = ImageDraw.Draw(overlay_img)
        overlay_draw.rectangle([left, top, right, bottom], fill=(0, 255, 0, 80))
        overlay_draw.rectangle([left, top, right, bottom], outline=(0, 0, 255, 255), width=4)

        # Composite
        img_rgba = img.convert('RGBA')
        img_rgba = Image.alpha_composite(img_rgba, overlay_img)
        img_result = img_rgba.convert('RGB')

        # Draw on result
        draw = ImageDraw.Draw(img_result)

        # Draw blue border
        draw.rectangle([left, top, right, bottom], outline="blue", width=4)

        # Calculate center
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2

        # Draw red crosshair at center
        crosshair_size = 10
        draw.line([(center_x - crosshair_size, center_y),
                   (center_x + crosshair_size, center_y)], fill="red", width=2)
        draw.line([(center_x, center_y - crosshair_size),
                   (center_x, center_y + crosshair_size)], fill="red", width=2)

        # Draw circle around center
        draw.ellipse([center_x - 5, center_y - 5, center_x + 5, center_y + 5],
                    outline="red", fill="red")

        # Add label at top
        label_bg = [0, max(0, top - 35), min(img_result.width, right), top]
        draw.rectangle(label_bg, fill="black")
        label_text = f"{target_description}"
        if confidence is not None:
            label_text += f" ({int(confidence*100)}%)"
        draw.text((left + 5, max(0, top - 30)), label_text, fill="yellow")

        # Add coordinates label
        coord_text = f"({center_x}, {center_y})"
        coord_y = max(0, top - 20) if top > 20 else bottom + 5
        draw.text((center_x - 30, coord_y), coord_text, fill="white")

        # Save annotated image
        working_dir = get_working_root()
        os.makedirs(working_dir, exist_ok=True)
        annotated_filename = f"annotated_{os.path.basename(screenshot_path)}"
        annotated_path = os.path.join(working_dir, annotated_filename)
        img_result.save(annotated_path)

        # Encode to base64 so AI sees the result automatically
        image_base64 = _encode_image_to_base64(annotated_path)

        # Calculate box dimensions
        box_width = right - left
        box_height = bottom - top

        return {
            "annotated_image": annotated_path,
            "center_x": center_x,
            "center_y": center_y,
            "box_info": {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "width": box_width,
                "height": box_height,
                "area": box_width * box_height
            },
            "image_base64": image_base64,
            "message": f"Target '{target_description}' annotated. "
                      f"Center: ({center_x}, {center_y}), "
                      f"Box: {box_width}x{box_height}px"
        }

    except Exception as e:
        return {
            "annotated_image": None,
            "center_x": None,
            "center_y": None,
            "box_info": None,
            "message": f"Error drawing annotation: {str(e)}"
        }

def _verify_coordinates_valid(target_box, image_width, image_height):
    """
    [UNUSED HELPER - not called by any code path] Bounds-check a bounding box.

    Verifies that the given target_box lies inside the image and has a plausible
    size (>= 5px sides, <= 80% of an image dimension).

    Args:
        target_box: [left, top, right, bottom]
        image_width: Image width in pixels
        image_height: Image height in pixels

    Returns:
        dict with validation results
    """
    if target_box is None:
        return {"valid": False, "reason": "No target box provided"}

    left, top, right, bottom = target_box

    # Check bounds
    if left < 0 or right > image_width:
        return {"valid": False, "reason": f"X coordinates out of bounds (0-{image_width})"}
    if top < 0 or bottom > image_height:
        return {"valid": False, "reason": f"Y coordinates out of bounds (0-{image_height})"}

    # Check reasonable size
    box_width = right - left
    box_height = bottom - top

    if box_width < 5 or box_height < 5:
        return {"valid": False, "reason": f"Box too small: {box_width}x{box_height}px"}

    if box_width > image_width * 0.8 or box_height > image_height * 0.8:
        return {"valid": False, "reason": f"Box suspiciously large: {box_width}x{box_height}px"}

    # Calculate center
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2

    return {
        "valid": True,
        "center_x": center_x,
        "center_y": center_y,
        "box_width": box_width,
        "box_height": box_height,
        "reason": "Coordinates valid and within bounds"
    }
