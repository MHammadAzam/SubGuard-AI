"""SQLite Database and Persistence Configuration for SubGuard AI.

Provides a thread-safe, zero-external-dependency SQLite persistence layer
for local demonstration and development.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Generator


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "subguard.db"
CURRENT_SCHEMA_VERSION = 2


def get_db_path() -> Path:
    """Return configured SQLite database file path."""
    env_path = os.getenv("SUBGUARD_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Create and return a configured sqlite3 connection with row factory and foreign keys."""
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def get_schema_version(db_path: Path | None = None) -> int:
    """Return the SQLite PRAGMA user_version integer for the database."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("PRAGMA user_version;").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS submissions (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        overall_status TEXT NOT NULL,
        processing_status TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'mock',
        evaluation_date TEXT NOT NULL,
        subcontractor_name TEXT,
        result_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_submissions_created_at ON submissions(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(overall_status)",
    """
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        submission_id TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        document_type TEXT NOT NULL,
        storage_path TEXT NOT NULL,
        file_size_bytes INTEGER NOT NULL,
        page_count INTEGER NOT NULL DEFAULT 0,
        upload_timestamp TEXT NOT NULL,
        processing_status TEXT NOT NULL,
        error_message TEXT,
        FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_documents_submission ON documents(submission_id)",
    """
    CREATE TABLE IF NOT EXISTS review_records (
        id TEXT PRIMARY KEY,
        submission_id TEXT NOT NULL,
        finding_id TEXT,
        reason_code TEXT,
        review_status TEXT NOT NULL,
        reviewer_notes TEXT NOT NULL,
        reviewer_name TEXT NOT NULL DEFAULT 'Compliance Reviewer',
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reviews_submission ON review_records(submission_id)",
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('viewer', 'analyst', 'reviewer', 'admin')),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        token_kind TEXT NOT NULL DEFAULT 'session' CHECK (token_kind IN ('session', 'api_token')),
        label TEXT,
        csrf_token TEXT,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)",
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        description TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
)


def _apply_schema_migrations(conn: sqlite3.Connection, current_uv: int) -> None:
    """Apply baseline DDL and additive migrations inside an open transaction."""
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)

    existing_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(review_records)").fetchall()
    }
    if "reviewer_user_id" not in existing_cols:
        conn.execute("ALTER TABLE review_records ADD COLUMN reviewer_user_id TEXT;")

    if current_uv < 1:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, description, applied_at) "
            "VALUES (1, 'Baseline compliance schema', datetime('now'))"
        )
    if current_uv < CURRENT_SCHEMA_VERSION:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, description, applied_at) "
            "VALUES (?, 'Phase 2-3B auth, sessions, audit attribution, and schema versioning', datetime('now'))",
            (CURRENT_SCHEMA_VERSION,),
        )
        conn.execute(f"PRAGMA user_version = {int(CURRENT_SCHEMA_VERSION)};")


def init_db(db_path: Path | None = None) -> None:
    """Initialize database tables, indexes, and constraints inside an atomic transaction."""
    conn = get_connection(db_path)
    try:
        current_uv = int(conn.execute("PRAGMA user_version;").fetchone()[0])
        if current_uv > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"Incompatible database schema version {current_uv}: "
                f"application supports up to version {CURRENT_SCHEMA_VERSION}."
            )

        conn.execute("BEGIN IMMEDIATE;")
        try:
            _apply_schema_migrations(conn, current_uv)
            conn.execute("COMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()
