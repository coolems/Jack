"""
Clear all log files on server startup.
This ensures we start with a clean log slate.
"""
from pathlib import Path

import logging
logger = logging.getLogger(__name__)

def clear_logs():
    """Remove all log files from the logs directory."""
    log_dir = Path(__file__).parent.parent / "logs"
    
    if not log_dir.exists():
        log_dir.mkdir(parents=True, exist_ok=True)
        return
    
    # Count files before deletion
    log_files = list(log_dir.glob("*.log"))
    if log_files:
        for log_file in log_files:
            try:
                log_file.unlink()
            except Exception as e:
                logger.debug("Non-critical exception caught at CLIENT/utils/clear_logs.py:21")
    else:
        pass

if __name__ == "__main__":
    clear_logs()
