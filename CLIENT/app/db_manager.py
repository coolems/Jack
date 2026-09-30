"""
Centralized SQLite database manager.

Provides:
    - Helper to create connections with WAL mode and proper settings enabled
    - Ensures every connection has safe defaults (busy_timeout, foreign_keys)
    - No shared connections - each request gets its own safe connection
    - Proper cleanup guidance for all callers

Why not a shared connection?
    SQLite connections are NOT thread-safe. FastAPI runs concurrent requests,
    so sharing one connection would cause ProgrammingError or data corruption.
    
    Instead, we create NEW connections per-request but ensure:
    1. WAL journal mode is set (DB-level setting, persists across connections)
    2. Busy timeout prevents immediate failures on lock contention
    3. Foreign keys are enforced
    4. Consistent safe defaults everywhere

Usage:
    # Initialize WAL mode once at startup
    init_db_manager("/path/to/database.db")

    # Get a SAFE new connection anywhere in the app
    from app.db_manager import get_db_connection, close_db_connection
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT ...")
    finally:
        close_db_connection(conn)
"""

import sqlite3

import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("COOLEMS.DBManager")

# - Module-level state -------------------------------------------------
_db_path: Optional[str] = None
_initialized = False

def init_db_manager(db_path: str) -> None:
    """
    Initialize the database manager. Sets WAL mode and safe defaults on the DB.

    This MUST be called once at application startup before any DB operations.
    Subsequent calls are no-ops (safe to call from multiple places).

    Args:
        db_path: Absolute path to the SQLite database file.
    """
    global _db_path, _initialized

    if _initialized:
        return  # Already initialized — skip silently

    _db_path = db_path

    # Apply DB-level settings (WAL mode persists across connections)
    conn = sqlite3.connect(_db_path)
    try:
        # -- Enable WAL mode for concurrent read/write ------------------
        conn.execute("PRAGMA journal_mode=WAL")
        logger.info(f"Set journal mode to WAL (path: {_db_path})")

        # -- Enable foreign key enforcement -----------------------------
        conn.execute("PRAGMA foreign_keys=ON")
        logger.info("Enabled foreign key constraints")

        # -- Synchronous mode: NORMAL is safe with WAL and faster -------
        conn.execute("PRAGMA synchronous=NORMAL")
        logger.info("Set synchronous to NORMAL")

        _initialized = True
        logger.info(f"Database manager initialized: {_db_path}")
    except sqlite3.Error as e:
        logger.error(f"Failed to initialize DB manager: {e}")
        raise
    finally:
        conn.close()

def get_db_connection() -> sqlite3.Connection:
    """
    Create a NEW safe database connection with proper settings.

    Each call creates a fresh connection configured with:
    - Busy timeout (5 seconds) to handle lock contention gracefully
    - Foreign key enforcement enabled
    - WAL mode inherited from DB-level setting

    Raises:
        RuntimeError: If init_db_manager() has not been called yet.

    Returns:
        A new sqlite3.Connection object ready for use.
    """
    global _db_path, _initialized

    if not _initialized or _db_path is None:
        raise RuntimeError(
            "Database manager not initialized! Call init_db_manager() at startup."
        )

    # Create a fresh connection for this request
    conn = sqlite3.connect(_db_path)

    # Per-connection settings (must be set on every new connection)
    conn.execute("PRAGMA busy_timeout=5000")  # Wait up to 5s if DB locked
    conn.execute("PRAGMA foreign_keys=ON")     # Enforce FK constraints

    return conn

def close_db_connection(conn) -> None:
    """
    Close a database connection safely.

    Call this in finally blocks to ensure connections are always released.

    Note: sqlite3.Connection does NOT have a .closed attribute (unlike psycopg2).
    We use try/except to handle already-closed or invalid connections gracefully.

    Args:
        conn: The connection object returned by get_db_connection().
    """
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass  # Connection already closed or invalid


@contextmanager
def db_connection():
    """
    Context manager for safe database access.

    Usage:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ...")

    The connection is always closed properly, even on exceptions.
    """
    conn = get_db_connection()
    try:
        yield conn
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        raise
    finally:
        close_db_connection(conn)

def shutdown_db_manager() -> None:
    """
    Safely shut down the database manager.

    Call this on application exit to ensure WAL checkpointing completes.
    This is a no-op since we don't maintain persistent connections,
    but provides a clean shutdown hook for future enhancements.
    """
    global _initialized

    # Final checkpoint to merge WAL into main DB
    if _db_path:
        try:
            conn = sqlite3.connect(_db_path)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logger.info("Final WAL checkpoint completed on shutdown")
            conn.close()
        except sqlite3.Error as e:
            logger.warning(f"WAL checkpoint failed during shutdown: {e}")

    _initialized = False
    logger.info("Database manager shut down")

def get_db_path() -> Optional[str]:
    """Return the configured database path, or None if not initialized."""
    return _db_path
