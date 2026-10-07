"""SQLite schema initialization and migrations for COOLEMS CLIENT.

Creates the conversations/messages tables on first run (anchored to
config.DB_PATH) and applies additive column migrations (file_contents,
ip_address, color). All connections go through app.db_manager
(WAL mode, busy timeout, foreign keys enabled).
"""

import sqlite3
import logging

from config import MODEL_NAME
from app.db_manager import get_db_connection, init_db_manager

logger = logging.getLogger("COOLEMS.Database")


def _safe_sql_literal(value: str) -> str:
    """Escape a string for safe use in SQL literal (handles quotes, slashes, colons)."""
    return "'" + value.replace("'", "''") + "'"


def init_db(db_path: str):
    """Initialize the database with required tables."""
    logger.info("Initializing database...")

    # Initialize db_manager first (sets WAL mode and safe defaults)
    try:
        init_db_manager(db_path)
    except Exception:
        pass  # Already initialized - continue with existing connection

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
        table_exists = cursor.fetchone()

        if not table_exists:
            model_default = _safe_sql_literal(MODEL_NAME)
            cursor.execute(f"""
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    model TEXT DEFAULT {model_default},
                    system_prompt TEXT DEFAULT 'You are an AI assistant.',
                    ip_address TEXT DEFAULT 'unknown',
                    working_root TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    role TEXT,
                    content TEXT,
                    media_urls TEXT,
                    file_contents TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations (id)
                )
            """)
            logger.info("Created fresh database tables")
        else:
            cursor.execute("PRAGMA table_info(messages)")
            cols = [c[1] for c in cursor.fetchall()]
            if "file_contents" not in cols:
                cursor.execute("ALTER TABLE messages ADD COLUMN file_contents TEXT")
                logger.info("Added file_contents column to messages")
            cursor.execute("PRAGMA table_info(conversations)")
            conv_cols = [c[1] for c in cursor.fetchall()]
            # Legacy per-conversation agent mode was removed (2026-08-24): it is now a
            # global UI toggle, so drop the old column from pre-existing databases.
            if "agent_mode" in conv_cols:
                cursor.execute("ALTER TABLE conversations DROP COLUMN agent_mode")
                logger.info("Dropped legacy per-conversation agent_mode column (now a global UI toggle)")
            if "ip_address" not in conv_cols:
                cursor.execute("ALTER TABLE conversations ADD COLUMN ip_address TEXT DEFAULT 'unknown'")
                logger.info("Added ip_address column to conversations")
            # Per-chat working root (2026-08-29): stores which working folder was used for this chat.
            # NULL = legacy chat -> clicking it keeps the currently active working root (no-op).
            if "working_root" not in conv_cols:
                cursor.execute("ALTER TABLE conversations ADD COLUMN working_root TEXT DEFAULT NULL")
                logger.info("Added working_root column to conversations")
        conn.commit()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise
    finally:
        conn.close()

# ---- Conversation color & sort support (added 2025-07-12) ----

def migrate_conversation_colors(db_path: str):
    """
    Migration: Add optional 'color' column to conversations table.

    The color column stores one of 8 predefined color names (e.g. 'red', 'blue').
    NULL means no color assigned (default / transparent).

    Safe to call on existing databases - uses ALTER TABLE with exception handling.
    """
    # Ensure db_manager is initialized
    try:
        init_db_manager(db_path)
    except Exception:
        pass  # Already initialized

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("ALTER TABLE conversations ADD COLUMN color TEXT DEFAULT NULL")
            logger.info("Added 'color' column to conversations table")
        except sqlite3.OperationalError:
            logger.info("'color' column already exists in conversations")
        conn.commit()
    except Exception as e:
        logger.error(f"Color migration failed: {e}")
        raise
    finally:
        conn.close()
