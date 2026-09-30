"""Target Area Validation Tool - validate_target_area()

Purpose: answer "am I about to click on the RIGHT thing?" before moving the mouse.

What this tool ACTUALLY does (no vision model is called inside it):
1. Validates the screenshot path inside working_root (path_guard)
2. Reads the image dimensions
3. Stamps a black text banner ("TARGET: <description>" + size) across the top of
   the screenshot and saves it as target_<name> in working_root
4. Returns the annotated image path, the image dimensions, and next-step guidance

The AI (Jack) then views the annotated image with view_image(), locates the target
itself, and decides the click coordinates. This tool does NOT draw a bounding box
and does NOT return any coordinates or confidence - those come from your own review
of the screenshot.

Functions:
- validate_target_area: main entry point (delivered to clients)
- _resolve_screenshot_path: path-safety helper (find_file + path_guard fallback)
- draw_target_box: stamps the text banner and saves the annotated copy
  (legacy name - it draws a banner, not a box; kept for compatibility)
- calculate_click_center: standalone utility, currently NOT called by any code path
- verify_target_before_click: standalone utility, currently NOT called by any code path
"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "validate_target_area",
        "description": "Marks a target area on a browser screenshot before mouse movement. Stamps the target description as a text banner on the screenshot, returns the annotated image path and image dimensions so you can visually confirm the right element BEFORE moving the mouse. No vision call is made; view the annotated image (view_image) and determine click coordinates yourself.",
        "parameters": {
            "type": "object",
            "properties": {
                "screenshot_path": {"type": "string", "description": "Path to the screenshot image inside working_root, e.g. from browser_screenshot()."},
                "target_description": {"type": "string", "description": "What element you intend to click, e.g. 'search button' or 'login field'. Stamped on the annotated image."},
                "confidence_threshold": {"type": "number", "description": "Accepted for API compatibility; unused - this tool makes no vision call and has no confidence score."}
            },
            "required": ["screenshot_path", "target_description"]
        }
    }
}

import os

from ..utils import get_working_root, find_file, validate_file_access


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


def validate_target_area(screenshot_path, target_description, confidence_threshold=0.7):
    """
    Mark a target area on a screenshot for visual confirmation before mouse movement.

    What it does:
    1. Validates ``screenshot_path`` inside working_root (path_guard)
    2. Reads the image dimensions
    3. Stamps a text banner ("TARGET: <description>" + size) on the screenshot and
       saves the annotated copy as ``target_<name>`` in working_root
    4. Returns the annotated path, image dimensions, and next-step guidance

    No vision model is called and NO coordinates are returned - you (the AI) view
    the annotated image with view_image() and decide where to click.

    SECURITY: All screenshot paths are validated inside working_root before access.

    Args:
        screenshot_path: Path to the screenshot image (validated inside working_root)
        target_description: What element you intend to click (e.g., "search button")
        confidence_threshold: Accepted for API compatibility; unused - this tool
            makes no vision call and therefore has no confidence score.

    Returns:
        dict with:
        - success: bool
        - image_dimensions: {"width": int, "height": int}
        - annotated_image: Path to the banner-stamped copy in working_root
        - message: Human-readable status
        - next_step: Guidance for confirming before moving the mouse

        On failure (path security / file errors): success=False plus None/0.0
        placeholders (target_box, center_x, center_y, annotated_image, confidence)
        kept for schema stability - they are always None/0.0 and must not be used.
    """
    try:
        # FIX (2026-08-23): lazy Pillow import -- a top-level `from PIL import ...`
        # made this tool silently UNAVAILABLE at compile time in venvs without
        # Pillow. Now a missing dependency produces a clean, honest tool result.
        try:
            from PIL import Image  # noqa: F401 (used below)
        except ImportError as e:
            return {
                "success": False,
                "target_box": None,
                "center_x": None,
                "center_y": None,
                "annotated_image": None,
                "confidence": 0.0,
                "message": f"Pillow not installed ({e}). Run: pip install Pillow"
            }

        # --- SECURITY: validate screenshot path inside working_root ---------
        safe_path = _resolve_screenshot_path(screenshot_path)

        # Get image dimensions for context
        img = Image.open(safe_path)
        img_width, img_height = img.size

        # Draw target box on screenshot
        annotated_path = draw_target_box(safe_path, target_description)

        return {
            "success": True,
            "image_dimensions": {"width": img_width, "height": img_height},
            "annotated_image": annotated_path,
            "message": f"Target area for '{target_description}' marked on screenshot. "
                      f"Review the annotated image to confirm before moving mouse. "
                      f"Image size: {img_width}x{img_height}",
            "next_step": "Use view_image() on annotated_image to visually confirm the target, "
                        "determine click coordinates yourself, then move the mouse with set_pos()"
        }

    except (ValueError, FileNotFoundError) as e:
        # Security / existence errors -- surface directly
        return {
            "success": False,
            "target_box": None,
            "center_x": None,
            "center_y": None,
            "annotated_image": None,
            "confidence": 0.0,
            "message": str(e)
        }
    except Exception as e:
        return {
            "success": False,
            "target_box": None,
            "center_x": None,
            "center_y": None,
            "annotated_image": None,
            "confidence": 0.0,
            "message": f"Error validating target: {str(e)}"
        }

def draw_target_box(screenshot_path, target_description):
    """
    Stamp a text banner on the screenshot marking the intended target.

    Draws a black banner across the top with "TARGET: <description>" (yellow) and
    the image size (white), then saves the annotated copy as ``target_<name>`` in
    working_root. Despite the legacy name, NO bounding box is drawn - this tool has
    no vision step that would produce one.

    Args:
        screenshot_path: Path to the original screenshot (already validated)
        target_description: Description of what we are targeting

    Returns:
        Path to the annotated image; falls back to the original path if annotation fails.
    """
    try:
        from PIL import Image, ImageDraw  # lazy -- see validate_target_area note (2026-08-23)
        img = Image.open(screenshot_path)
        draw = ImageDraw.Draw(img)

        # Add annotation text at top
        draw.rectangle([0, 0, img.width, 40], fill="black")
        draw.text((10, 10), f"TARGET: {target_description}", fill="yellow")
        draw.text((10, 25), f"Size: {img.width}x{img.height}", fill="white")

        # Save annotated image to working_root
        working_dir = get_working_root()
        os.makedirs(working_dir, exist_ok=True)
        annotated_filename = f"target_{os.path.basename(screenshot_path)}"
        annotated_path = os.path.join(working_dir, annotated_filename)
        img.save(annotated_path)

        return annotated_path

    except Exception as e:
        return screenshot_path  # Return original if annotation fails

def calculate_click_center(target_box):
    """
    Calculate the center point of a target bounding box (standalone utility).

    NOTE: currently NOT called by any tool or server/client code path - kept as a
    helper for manual/interactive use with boxes you determine yourself.

    For buttons: center is usually best
    For text fields: slightly left of center for cursor placement
    For links: center works

    Args:
        target_box: [left, top, right, bottom]

    Returns:
        dict with center_x, center_y, and box metadata
    """
    if target_box is None:
        return {"center_x": None, "center_y": None, "error": "No target box provided"}

    left, top, right, bottom = target_box

    # Calculate center
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2

    # Calculate box dimensions
    box_width = right - left
    box_height = bottom - top

    return {
        "center_x": center_x,
        "center_y": center_y,
        "box_width": box_width,
        "box_height": box_height,
        "box_area": box_width * box_height,
        "recommendation": "Move mouse to center coordinates, verify visually, then click"
    }

def verify_target_before_click(screenshot_path, expected_target_area=None):
    """
    Pre-click verification image: re-render a previously identified area for visual check.

    NOTE: currently NOT called by any tool or server/client code path - kept as a
    helper for manual/interactive use. Draws the given [left, top, right, bottom]
    area in red with a "PREVIOUS TARGET" label and saves verify_<name> in working_root.

    This catches cases where:
    - Page scrolled
    - Modal appeared
    - Element moved
    - Popup covered the target

    SECURITY: Validates screenshot path inside working_root before access.

    Args:
        screenshot_path: Current screenshot
        expected_target_area: Previously identified [left, top, right, bottom]

    Returns:
        dict with verification status
    """
    try:
        from PIL import Image, ImageDraw  # lazy -- see validate_target_area note (2026-08-23)
        # --- SECURITY: validate screenshot path -----------------------------
        safe_path = _resolve_screenshot_path(screenshot_path)

        img = Image.open(safe_path)
        draw = ImageDraw.Draw(img)

        if expected_target_area:
            # Draw the previously identified area to verify
            draw.rectangle(expected_target_area, outline="red", width=3)
            draw.text((expected_target_area[0], max(0, expected_target_area[1] - 20)),
                     "PREVIOUS TARGET", fill="red")

        working_dir = get_working_root()
        os.makedirs(working_dir, exist_ok=True)
        verified_path = os.path.join(working_dir, f"verify_{os.path.basename(safe_path)}")
        img.save(verified_path)

        return {
            "success": True,
            "verified_image": verified_path,
            "message": "Pre-click verification image created. Review to confirm target is still valid."
        }

    except (ValueError, FileNotFoundError) as e:
        return {
            "success": False,
            "message": str(e)
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Verification failed: {str(e)}"
        }
