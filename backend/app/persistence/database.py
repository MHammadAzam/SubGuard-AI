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
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Initialize database tables, indexes, and constraints if they do not exist."""
    conn = get_connection(db_path)
    try:
        with conn:
            conn.executescript(
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
                );

                CREATE INDEX IF NOT EXISTS idx_submissions_created_at ON submissions(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(overall_status);

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
                );

                CREATE INDEX IF NOT EXISTS idx_documents_submission ON documents(submission_id);

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
                );

                CREATE INDEX IF NOT EXISTS idx_reviews_submission ON review_records(submission_id);
                """
            )
    finally:
        conn.close()
