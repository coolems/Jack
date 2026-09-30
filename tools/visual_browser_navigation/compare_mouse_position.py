"""Compare mouse position between two screenshots by detecting the red cursor circle."""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "compare_mouse_position",
        "description": "Compare two screenshots and detect the red circle (mouse cursor marker) in each. Returns the (x, y) position of the red circle in both images, whether the positions are different, and the pixel difference (dx, dy, distance). Use this to verify if the mouse actually moved where you expected after a set_pos() call. Both images must have been created by full_desktop_screenshot() which draws the red circle.",
        "parameters": {
            "type": "object",
            "properties": {
                "screenshot_before": {
                    "type": "string",
                    "description": "Path to the first (before) screenshot PNG file (created by full_desktop_screenshot)."
                },
                "screenshot_after": {
                    "type": "string",
                    "description": "Path to the second (after) screenshot PNG file (created by full_desktop_screenshot)."
                },
                "tolerance": {
                    "type": "integer",
                    "description": "Maximum allowed distance in pixels to still count as 'same position'. Default 15."
                }
            },
            "required": [
                "screenshot_before",
                "screenshot_after"
            ]
        }
    }
}

import logging
import math
import os

from ..utils import get_working_root, find_file, validate_file_access

logger = logging.getLogger("COOLEMS.Tools.VisualBrowserNavigation")


def _resolve_screenshot_path(path_str: str, label: str) -> str:
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
        path_str, get_working_root(), label=label
    )


def compare_mouse_position(screenshot_before: str, screenshot_after: str, tolerance: int = 15) -> str:
    """
    Compare two screenshots to find the red cursor circle in each and report position difference.
    
    The red circle is drawn by full_desktop_screenshot() with:
    - Red outline (RGB 255, 0, 0)
    - Radius of ~30 pixels
    - Crosshair lines
    
    This tool uses pure pixel analysis (no vision model) to find the red circle center
    in both images and calculates the difference.
    
    Args:
        screenshot_before: Path to the 'before' screenshot PNG
        screenshot_after:  Path to the 'after' screenshot PNG
        tolerance:         Max pixel distance to still be considered 'same position'
    
    Returns:
        Formatted string with positions, difference, and whether mouse moved.
    """
    try:
        from PIL import Image

        # --- SECURITY: validate BOTH paths inside working_root before access --
        safe_before = _resolve_screenshot_path(screenshot_before, "Screenshot (before)")
        safe_after = _resolve_screenshot_path(screenshot_after, "Screenshot (after)")

        # --- Helper: find red circle center in an image ---
        def find_red_circle_center(img_path: str):
            """
            Find the center of the red circle drawn by full_desktop_screenshot().
            
            Strategy:
            1. Load image as RGB
            2. Scan for red pixels (high R, low G, low B)
            3. The red circle has width=3 outline, so we look for clusters of red pixels
            4. Return the centroid of all red pixels = approximate center
            
            Fallback: if no red found, return None
            """
            img = Image.open(img_path).convert("RGB")
            width, height = img.size
            pixels = img.load()
            
            red_pixels = []
            
            # Scan with stride for speed (check every 2nd pixel)
            stride = 2
            for y in range(0, height, stride):
                for x in range(0, width, stride):
                    r, g, b = pixels[x, y]
                    # Red circle is pure red: R > 200, G < 50, B < 50
                    if r > 200 and g < 50 and b < 50:
                        red_pixels.append((x, y))
            
            if not red_pixels:
                return None, f"No red pixels found in {img_path}"
            
            # Calculate centroid
            avg_x = sum(p[0] for p in red_pixels) / len(red_pixels)
            avg_y = sum(p[1] for p in red_pixels) / len(red_pixels)
            
            return (round(avg_x), round(avg_y)), None
        
        # --- Find red circle in both images ---
        pos_before, err_before = find_red_circle_center(safe_before)
        if err_before:
            return f"ERROR reading before screenshot: {err_before}"
        
        pos_after, err_after = find_red_circle_center(safe_after)
        if err_after:
            return f"ERROR reading after screenshot: {err_after}"
        
        # --- Calculate difference ---
        dx = pos_after[0] - pos_before[0]
        dy = pos_after[1] - pos_before[1]
        distance = math.sqrt(dx * dx + dy * dy)
        
        same_position = distance <= tolerance
        
        # --- Build result ---
        status = "SAME POSITION" if same_position else "MOVED"
        
        result = (
            f"=== MOUSE POSITION COMPARISON ===\n"
            f"\n"
            f"Before: ({pos_before[0]}, {pos_before[1]})\n"
            f"After:  ({pos_after[0]}, {pos_after[1]})\n"
            f"\n"
            f"Difference:\n"
            f"  DX: {dx:+d} pixels (horizontal)\n"
            f"  DY: {dy:+d} pixels (vertical)\n"
            f"  Distance: {distance:.1f} pixels\n"
            f"  Tolerance: {tolerance} pixels\n"
            f"\n"
            f"Result: {status}\n"
        )
        
        if not same_position:
            direction = ""
            if abs(dx) > abs(dy):
                direction = "RIGHT" if dx > 0 else "LEFT"
            else:
                direction = "DOWN" if dy > 0 else "UP"
            
            result += f"\nMouse moved ~{direction} by {distance:.0f} pixels.\n"
            
            if distance > 100:
                result += f"LARGE MOVEMENT DETECTED - verify this was intentional.\n"
        
        result += f"\nScreenshots compared:\n"
        result += f"  Before: {safe_before}\n"
        result += f"  After:  {safe_after}\n"
        
        logger.info(
            f"Mouse position comparison: before={pos_before}, after={pos_after}, "
            f"distance={distance:.1f}, same={same_position}"
        )
        
        return result
    
    except (ValueError, FileNotFoundError) as e:
        # Security / existence errors -- surface directly
        logger.debug("Caught expected path error in compare_mouse_position")
        return str(e)
    except ImportError:
        return "ERROR: PIL (Pillow) not installed. Run: pip install Pillow"
    except Exception as e:
        logger.error(f"compare_mouse_position failed: {e}")
        return f"ERROR: {str(e)}"
