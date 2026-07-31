"""Data access layer for projects – SQLite backend."""

import logging
from datetime import datetime, timezone
from typing import Any

import sqlite3

logger = logging.getLogger(__name__)


class ProjectsRepository:
    """CRUD operations for the projects table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, name: str, path: str, owner: str) -> int:
        """Insert a new project and return its ID."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO projects (name, path, owner, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [name, path, owner, now, now],
        )
        project_id = cursor.lastrowid
        logger.info("Created project id=%d name=%s", project_id, name)
        return project_id

    def get_by_id(self, project_id: int) -> dict[str, Any] | None:
        """Retrieve a single project by its primary key (excluding soft-deleted)."""
        row = self.conn.execute(
            """
            SELECT id, name, path, owner, created_at, updated_at, deleted_at
            FROM projects
            WHERE id = ? AND deleted_at IS NULL
            """,
            [project_id],
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_all(
        self,
        owner: str,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return paginated projects for a given owner, excluding soft-deleted."""
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100

        params = [owner]
        cursor_cond = ""
        if cursor is not None:
            try:
                cursor_id = int(cursor)
                cursor_cond = " AND id > ?"
                params.append(cursor_id)
            except (ValueError, TypeError):
                logger.warning("Invalid cursor value ignored: %s", cursor)

        query = f"""
            SELECT id, name, path, owner, created_at, updated_at, deleted_at
            FROM projects
            WHERE owner = ? AND deleted_at IS NULL{cursor_cond}
            ORDER BY id ASC
            LIMIT ?
        """
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        projects = [self._row_to_dict(r) for r in rows]

        next_cursor = None
        if len(projects) == limit:
            next_cursor = str(projects[-1]["id"])

        return projects, next_cursor

    def update(self, project_id: int, **kwargs: Any) -> bool:
        """Update name and/or path for a project. Returns True if a row was affected."""
        allowed = {"name", "path"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return False

        set_clause = ", ".join(f"{col} = ?" for col in updates)
        values = list(updates.values())
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(project_id)

        cursor = self.conn.execute(
            f"""
            UPDATE projects
            SET {set_clause}, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            values,
        )
        return cursor.rowcount > 0

    def soft_delete(self, project_id: int) -> bool:
        """Mark a project as deleted (sets deleted_at)."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE projects
            SET deleted_at = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            [now, now, project_id],
        )
        return cursor.rowcount > 0

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a sqlite3.Row to a dictionary."""
        return dict(row)