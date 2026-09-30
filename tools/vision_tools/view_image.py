"""View image function - returns image path for vision model"""

__tool_description__ = {
    "type": "function",
    "function": {
        "name": "view_image",
        "description": "Load an image for vision model analysis. Returns image data that can be analyzed. Image file must be inside working_root directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Filename of the image to view. Can include folder path relative to working_root (e.g. 'my_image.png' or 'subfolder/my_image.png'). File must exist inside working_root."
                }
            },
            "required": ["filename"]
        }
    }
}

import logging
import base64

from ..utils import find_file, is_image_file

logger = logging.getLogger("COOLEMS.Tools.Vision")

def view_image(filename: str) -> dict:
    """
    Load an image and return a dict with image_base64 for vision model.
    The agentic loop checks for image_base64 in the result and sends it
    to Ollama via the 'images' key in the message, enabling true vision.

    Args:
        filename: Image file name. Can include relative folder path (e.g. 'screenshot.png' or 'images/screenshot.png').
                  File is searched for inside working_root using find_file().
    """
    logger.info(f"[VISION] Loading image: {filename}")
    
    filepath = find_file(filename)
    if not filepath:
        return {"status": "error",
            "message": f"Image file not found: {filename}"}
    
    if not is_image_file(filepath):
        return {"status": "error",
            "message": f"File is not an image: {filename}"}
    
    try:
        with open(filepath, 'rb') as f:
            image_data = base64.b64encode(f.read()).decode('utf-8')
        
        # Return dict with image_base64 so agentic loop sends it via 'images' key
        return {
            "image_base64": image_data,
            "file_path": filepath,
            "filename": filename,
        }
        
    except Exception as e:
        return {"status": "error",
            "message": f"Could not read image file: {str(e)}"}
