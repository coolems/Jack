"""
Clear all Python bytecode cache (.pyc files and __pycache__ directories).
This ensures we rebuild everything from source on server startup.
"""
import shutil
from pathlib import Path

import logging
logger = logging.getLogger(__name__)

def clear_pycache():
    """Remove all .pyc files and __pycache__ directories."""
    base_dir = Path(__file__).parent.parent
    
    
    # Find all __pycache__ directories
    pycache_dirs = list(base_dir.rglob("__pycache__"))
    
    # Find all .pyc files
    pyc_files = list(base_dir.rglob("*.pyc"))
    
    # Find all .pyo files (optimized bytecode)
    pyo_files = list(base_dir.rglob("*.pyo"))
    
    total_items = len(pycache_dirs) + len(pyc_files) + len(pyo_files)
    
    if total_items == 0:
        return
    
    
    # Remove __pycache__ directories
    for pycache_dir in pycache_dirs:
        try:
            shutil.rmtree(pycache_dir)
        except Exception as e:
            logger.debug("Cleanup/delete non-critical error at CLIENT/utils/clear_pycache.py:32")
    
    # Remove .pyc files
    for pyc_file in pyc_files:
        try:
            pyc_file.unlink()
        except Exception as e:
            logger.debug("Cleanup/delete non-critical error at CLIENT/utils/clear_pycache.py:39")
    
    # Remove .pyo files
    for pyo_file in pyo_files:
        try:
            pyo_file.unlink()
        except Exception as e:
            logger.debug("Cleanup/delete non-critical error at CLIENT/utils/clear_pycache.py:46")
    

if __name__ == "__main__":
    clear_pycache()
