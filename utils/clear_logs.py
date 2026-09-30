"""
Clear all log files on server startup.
This ensures we start with a clean log slate.
"""
from pathlib import Path

def clear_logs():
    """Remove all log files from the logs directory."""
    log_dir = Path(__file__).parent.parent / "logs"
    
    if not log_dir.exists():
        print("✓ Logs directory doesn't exist, creating it...")
        log_dir.mkdir(parents=True, exist_ok=True)
        return
    
    # Count files before deletion
    log_files = list(log_dir.glob("*.log"))
    if log_files:
        print(f"🗑️  Clearing {len(log_files)} log file(s)...")
        for log_file in log_files:
            try:
                log_file.unlink()
                print(f"  ✓ Removed: {log_file.name}")
            except Exception as e:
                print(f"  ✗ Failed to remove {log_file.name}: {e}")
        print(f"✓ Cleared all log files from {log_dir}")
    else:
        print("✓ No log files to clear")

if __name__ == "__main__":
    clear_logs()
