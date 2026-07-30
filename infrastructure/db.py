"""DuckDB connection management and database migrations."""

import logging
from contextlib import contextmanager
from pathlib import Path

import duckdb

from gitpilot.domain.settings import get_gitpilot_dir

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
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
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    hash TEXT NOT NULL,
    message TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT 'main',
    committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_commits_project ON commits(project_id);
CREATE INDEX IF NOT EXISTS idx_commits_hash ON commits(hash);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    value JSON NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('string','integer','boolean','json')),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS discord_webhooks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_webhooks_project ON discord_webhooks(project_id);
"""


def get_db_path() -> Path:
    """Return the path to the DuckDB database file."""
    return get_gitpilot_dir() / "data.db"


def get_connection(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Create a new DuckDB connection to the database file.
    If db_path is None, uses the default location.
    """
    if db_path is None:
        db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def managed_connection(db_path: Path | None = None):
    """Context manager that yields a DuckDB connection and closes it afterward."""
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def run_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    """Run all pending schema migrations."""
    logger.info("Running database migrations...")
    try:
        conn.execute(SCHEMA_SQL)
        conn.commit()
        logger.info("Migrations completed successfully")
    except Exception as exc:
        logger.error("Migration failed: %s", exc)
        raise


def initialize_database(db_path: Path | None = None) -> None:
    """Initialize the database: create tables if not exist."""
    with managed_connection(db_path) as conn:
        run_migrations(conn)