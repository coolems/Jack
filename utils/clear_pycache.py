"""
Clear all Python bytecode cache (.pyc files and __pycache__ directories).
This ensures we rebuild everything from source on server startup.
"""
import shutil
from pathlib import Path

def clear_pycache():
    """Remove all .pyc files and __pycache__ directories."""
    base_dir = Path(__file__).parent.parent
    
    print("🔍 Scanning for Python cache files...")
    
    # Find all __pycache__ directories
    pycache_dirs = list(base_dir.rglob("__pycache__"))
    
    # Find all .pyc files
    pyc_files = list(base_dir.rglob("*.pyc"))
    
    # Find all .pyo files (optimized bytecode)
    pyo_files = list(base_dir.rglob("*.pyo"))
    
    total_items = len(pycache_dirs) + len(pyc_files) + len(pyo_files)
    
    if total_items == 0:
        print("✓ No Python cache files found")
        return
    
    print(f"🗑️  Found {total_items} cache item(s) to remove:")
    print(f"  - {len(pycache_dirs)} __pycache__ directories")
    print(f"  - {len(pyc_files)} .pyc files")
    print(f"  - {len(pyo_files)} .pyo files")
    
    # Remove __pycache__ directories
    for pycache_dir in pycache_dirs:
        try:
            shutil.rmtree(pycache_dir)
            print(f"  ✓ Removed: {pycache_dir.relative_to(base_dir)}")
        except Exception as e:
            print(f"  ✗ Failed to remove {pycache_dir}: {e}")
    
    # Remove .pyc files
    for pyc_file in pyc_files:
        try:
            pyc_file.unlink()
            print(f"  ✓ Removed: {pyc_file.relative_to(base_dir)}")
        except Exception as e:
            print(f"  ✗ Failed to remove {pyc_file}: {e}")
    
    # Remove .pyo files
    for pyo_file in pyo_files:
        try:
            pyo_file.unlink()
            print(f"  ✓ Removed: {pyo_file.relative_to(base_dir)}")
        except Exception as e:
            print(f"  ✗ Failed to remove {pyo_file}: {e}")
    
    print(f"✓ Cleared all Python cache from {base_dir}")

if __name__ == "__main__":
    clear_pycache()
