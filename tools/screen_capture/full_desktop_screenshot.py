"""Full Desktop Screenshot - captures the entire desktop with all apps, windows, icons and taskbar"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "full_desktop_screenshot",
        "description": "Take a screenshot of the ENTIRE desktop screen - all windows, applications, desktop icons, and taskbar. This is NOT just the browser. Saves the screenshot as a PNG in the working_root folder. Automatically draws a red circle on the mouse cursor position. Returns the image data so you can see what's on screen directly.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Optional filename for the screenshot (without extension). Auto-generates timestamp-based name if not provided."
                },
                "region": {
                    "type": "string",
                    "description": "Optional region to capture as 'x,y,width,height'. If not provided, captures entire screen."
                }
            },
            "required": []
        }
    }
}

import logging
import os
import datetime
import base64

from ..utils import get_working_root, validate_filename, guard_path_inside_working_root

logger = logging.getLogger("COOLEMS.Tools.ScreenCapture")

def full_desktop_screenshot(filename: str = None, region: str = None) -> dict:
    """
    Take a screenshot of the ENTIRE desktop - all windows, apps, icons, taskbar - with cursor marker.
    
    Args:
        filename: Optional filename (without extension). Auto-generates if not provided.
        region: Optional region as 'x,y,width,height'. Captures entire screen if not provided.
    
    Returns:
        Dict with image_base64 (for vision), screenshot_path, file_size, and status.
    """
    # FIX (2026-08-23): check Pillow FIRST. On Windows, importing pyautogui without
    # Pillow raises a misleading error that blames PyAutoGUI/pyscreeze -- checking PIL
    # up front reports the REAL missing dependency honestly.
    try:
        from PIL import ImageDraw  # noqa: F401 (used below via full import)
    except ImportError as e:
        return {
            "error": f"Pillow not installed ({e}). Run: pip install Pillow",
            "image_base64": None
        }

    try:
        import pyautogui
        from PIL import ImageDraw, ImageFont
        
        # Generate filename if not provided
        if not filename:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"screen_{timestamp}"
        else:
            # SECURITY (2026-07-15): user-supplied filenames must stay inside
            # working_root -- reject separators and traversal.
            try:
                validate_filename(filename, label="Screenshot filename")
            except ValueError as e:
                return {"error": f"Screenshot failed: {e}", "image_base64": None}
        
        if not filename.endswith('.png'):
            filename += '.png'
        
        # Ensure output directory exists (using dynamic working root)
        working_dir = get_working_root()
        os.makedirs(working_dir, exist_ok=True)
        filepath = os.path.normpath(os.path.join(working_dir, filename))

        # Final containment check -- belt and braces (realpath resolves symlinks/..)
        try:
            guard_path_inside_working_root(filepath, working_dir, label="Screenshot file path")
        except ValueError as e:
            return {"error": f"Screenshot failed: {e}", "image_base64": None}
        
        # Parse region if provided
        if region:
            parts = region.split(',')
            if len(parts) == 4:
                rx, ry, rw, rh = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                screenshot = pyautogui.screenshot(region=(rx, ry, rw, rh))
            else:
                return {
                    "error": f"Invalid region format. Use 'x,y,width,height'. Got: {region}",
                    "image_base64": None
                }
        else:
            screenshot = pyautogui.screenshot()
        
        # Draw red circle on mouse cursor position
        try:
            mouse_x, mouse_y = pyautogui.position()
            
            # Check if cursor is within the screenshot bounds
            img_width, img_height = screenshot.size
            
            # If region is specified, adjust coordinates
            if region:
                parts = region.split(',')
                rx, ry, rw, rh = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                mouse_x = mouse_x - rx
                mouse_y = mouse_y - ry
            
            # Only draw if cursor is within bounds
            if 0 <= mouse_x < img_width and 0 <= mouse_y < img_height:
                draw = ImageDraw.Draw(screenshot)
                
                # Draw a red circle around the cursor position
                circle_radius = 30
                draw.ellipse(
                    [mouse_x - circle_radius, mouse_y - circle_radius,
                     mouse_x + circle_radius, mouse_y + circle_radius],
                    outline="red",
                    width=3
                )
                
                # Draw crosshair lines
                line_length = 15
                draw.line(
                    [(mouse_x - line_length, mouse_y), (mouse_x + line_length, mouse_y)],
                    fill="red",
                    width=2
                )
                draw.line(
                    [(mouse_x, mouse_y - line_length), (mouse_x, mouse_y + line_length)],
                    fill="red",
                    width=2
                )
                
                # Draw cursor coordinates text
                try:
                    # Try to get a reasonable font
                    draw.text((mouse_x + circle_radius + 5, mouse_y - 10), 
                             f"({mouse_x},{mouse_y})", fill="red")
                except Exception:
                    pass  # Skip text if font not available
                
        except Exception as e:
            logger.debug("Non-critical exception caught at tools/screen_capture/full_desktop_screenshot.py:124")
            logger.warning(f"Could not draw cursor marker: {e}")
            pass
        
        # Save screenshot
        screenshot.save(filepath)
        file_size = os.path.getsize(filepath)
        
        # Encode image to base64 for vision
        with open(filepath, 'rb') as f:
            image_base64 = base64.b64encode(f.read()).decode('utf-8')
        
        return {
            "image_base64": image_base64,
            "screenshot_path": filepath,
            "file_size": file_size,
            "status": "success",
        }
    
    except ImportError as e:
        # Pillow already verified above -- an ImportError here is a genuine missing
        # pyautogui (or its platform deps), so report it accurately.
        return {
            "error": f"PyAutoGUI not installed ({e}). Run: pip install pyautogui",
            "image_base64": None
        }
    except Exception as e:
        return {
            "error": f"Screenshot failed: {str(e)}",
            "image_base64": None
        }
