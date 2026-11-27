"""SQLite schema initialization and migrations for COOLEMS CLIENT.

Creates the conversations/messages tables on first run (anchored to
config.DB_PATH) and applies additive column migrations (file_contents,
ip_address, color, parent_id, workspace_note). All connections go through
app.db_manager (WAL mode, busy timeout, foreign keys enabled).
"""

import logging

from client_config import MODEL_NAME
from app.db_manager import get_db_connection, init_db_manager
from app.workspace_tree import migrate_workspace_ids

logger = logging.getLogger("COOLEMS.Database")


TURN_LOG_SCHEMA = """
                CREATE TABLE IF NOT EXISTS turn_log (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    message_id TEXT DEFAULT NULL,
                    payload TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations (id)
                )
            """


def _create_turn_log_table(cursor) -> None:
    """Create the cycle-history table and its lookup index (idempotent).

    turn_log is a CHILD of conversations: one row per agentic turn, holding the
    collapsed cycle block the UI renders on reload. Because app/db_manager enforces
    foreign keys on every connection, deleting a workspace must clear these rows
    first - see app/websocket/turn_ops.delete_conversation_rows().
    """
    cursor.execute(TURN_LOG_SCHEMA)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_turn_log_conversation ON turn_log (conversation_id)"
    )


def _safe_sql_literal(value: str) -> str:
    """Escape a string for safe use in SQL literal (handles quotes, slashes, colons)."""
    return "'" + value.replace("'", "''") + "'"


def init_db(db_path: str):
    """Initialize the database with required tables."""
    logger.info("Initializing database...")

    init_db_manager(db_path)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

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
                    color TEXT DEFAULT NULL,
                    parent_id TEXT REFERENCES conversations(id) ON DELETE RESTRICT,
                    unread_finished INTEGER NOT NULL DEFAULT 0,
                    workspace_note TEXT DEFAULT NULL,
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
                    is_steer INTEGER DEFAULT 0,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations (id)
                )
            """)
            _create_turn_log_table(cursor)
            logger.info("Created fresh database tables")
        else:
            cursor.execute("PRAGMA table_info(messages)")
            cols = [c[1] for c in cursor.fetchall()]
            if "file_contents" not in cols:
                cursor.execute("ALTER TABLE messages ADD COLUMN file_contents TEXT")
                logger.info("Added file_contents column to messages")
            # (2026-11-08, user request) steering messages are LOOP WORK, not chat messages.
            # The flag is what lets the UI skip those rows on a reload and show them inside
            # the turn's loop-history block instead. Legacy rows stay 0 (real prompts).
            if "is_steer" not in cols:
                cursor.execute("ALTER TABLE messages ADD COLUMN is_steer INTEGER DEFAULT 0")
                logger.info("Added is_steer column to messages")
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
            if "color" not in conv_cols:
                cursor.execute("ALTER TABLE conversations ADD COLUMN color TEXT DEFAULT NULL")
                logger.info("Added color column to conversations")
            # Cycle history for pre-existing databases (added 2026-12-04).
            _create_turn_log_table(cursor)
            if "parent_id" not in conv_cols:
                cursor.execute(
                    "ALTER TABLE conversations ADD COLUMN parent_id TEXT "
                    "REFERENCES conversations(id) ON DELETE RESTRICT"
                )
            # (2026-11-24) persistent "finished & unread" flag: the sidebar glows a
            # workspace whose task ended until the user opens it. Legacy rows default to 0
            # (no surprise glow after upgrading).
            if "unread_finished" not in conv_cols:
                cursor.execute(
                    "ALTER TABLE conversations ADD COLUMN unread_finished INTEGER NOT NULL DEFAULT 0"
                )
                logger.info("Added unread_finished column to conversations")
            # (2026-11-27, user request) free-text "workspace info" note. It is a column of the
            # workspace ROW on purpose: the note is then deleted together with the workspace by
            # the plain DELETE in turn_ops.delete_conversation_rows - no child row to orphan.
            # Legacy rows stay NULL (the UI reads NULL as an empty note).
            if "workspace_note" not in conv_cols:
                cursor.execute("ALTER TABLE conversations ADD COLUMN workspace_note TEXT DEFAULT NULL")
                logger.info("Added workspace_note column to conversations")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_conversations_parent ON conversations(parent_id)")
        migrate_workspace_ids(cursor)
        conn.commit()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise
    finally:
        conn.close()
