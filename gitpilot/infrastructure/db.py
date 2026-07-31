"""SQLite database connection management and migrations – extended schema."""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from gitpilot.domain.settings import get_gitpilot_dir

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner);
CREATE INDEX IF NOT EXISTS idx_projects_deleted ON projects(deleted_at);

CREATE TABLE IF NOT EXISTS commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    hash TEXT NOT NULL,
    message TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT 'main',
    domain TEXT NOT NULL DEFAULT 'general',
    affected_symbols JSON DEFAULT '[]',
    optimization_notes JSON DEFAULT '[]',
    squash_candidate BOOLEAN DEFAULT FALSE,
    committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_commits_project ON commits(project_id);
CREATE INDEX IF NOT EXISTS idx_commits_hash ON commits(hash);
CREATE INDEX IF NOT EXISTS idx_commits_domain ON commits(domain);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value JSON NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('string','integer','boolean','json')),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS discord_webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_webhooks_project ON discord_webhooks(project_id);

-- NEW: learned user patterns (commit style, branch naming, etc.)
CREATE TABLE IF NOT EXISTS commit_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    pattern_type TEXT NOT NULL CHECK(pattern_type IN ('message_style','branch_prefix','debounce_pref','split_pref','commit_frequency')),
    value JSON NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    last_updated TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_patterns_owner ON commit_patterns(owner);
CREATE INDEX IF NOT EXISTS idx_patterns_type ON commit_patterns(pattern_type);

-- NEW: file co‑occurrence tracking for predictive fetching
CREATE TABLE IF NOT EXISTS file_associations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_a TEXT NOT NULL,
    file_b TEXT NOT NULL,
    co_occurrence INTEGER NOT NULL DEFAULT 1,
    last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, file_a, file_b)
);

CREATE INDEX IF NOT EXISTS idx_file_assoc_project ON file_associations(project_id);
CREATE INDEX IF NOT EXISTS idx_file_assoc_files ON file_associations(project_id, file_a);
"""


def get_db_path() -> Path:
    return get_gitpilot_dir() / "data.db"


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def managed_connection(db_path: Path | None = None):
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_migrations(conn: sqlite3.Connection) -> None:
    logger.info("Running database migrations...")
    try:
        conn.executescript(SCHEMA_SQL)
        logger.info("Migrations completed successfully")
    except Exception as exc:
        logger.error("Migration failed: %s", exc)
        raise


def initialize_database(db_path: Path | None = None) -> None:
    with managed_connection(db_path) as conn:
        run_migrations(conn)