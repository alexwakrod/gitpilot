"""Data access layer for commits."""

import logging
from datetime import datetime, timezone
from typing import Any

import duckdb

logger = logging.getLogger(__name__)


class CommitsRepository:
    """CRUD operations for the commits table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def create(
        self,
        project_id: int,
        hash: str,
        message: str,
        branch: str = "main",
        committed_at: str | None = None,
    ) -> int:
        """Insert a new commit record and return its ID."""
        if committed_at is None:
            committed_at = datetime.now(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()

        result = self.conn.execute(
            """
            INSERT INTO commits (project_id, hash, message, branch, committed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            [project_id, hash, message, branch, committed_at, now],
        )
        commit_id = result.fetchone()[0]
        self.conn.commit()
        logger.info("Recorded commit id=%d hash=%s", commit_id, hash[:8])
        return commit_id

    def get_by_id(self, commit_id: int) -> dict[str, Any] | None:
        """Retrieve a commit by ID (excluding soft-deleted)."""
        row = self.conn.execute(
            """
            SELECT id, project_id, hash, message, branch, committed_at, created_at, deleted_at
            FROM commits
            WHERE id = ? AND deleted_at IS NULL
            """,
            [commit_id],
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_by_project(
        self,
        project_id: int,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return paginated commits for a project, most recent first."""
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100

        params = [project_id]
        cursor_cond = ""
        if cursor is not None:
            try:
                cursor_id = int(cursor)
                cursor_cond = " AND id < ?"
                params.append(cursor_id)
            except (ValueError, TypeError):
                logger.warning("Invalid cursor value ignored: %s", cursor)

        query = f"""
            SELECT id, project_id, hash, message, branch, committed_at, created_at, deleted_at
            FROM commits
            WHERE project_id = ? AND deleted_at IS NULL{cursor_cond}
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        commits = [self._row_to_dict(r) for r in rows]

        next_cursor = None
        if len(commits) == limit:
            next_cursor = str(commits[-1]["id"])

        return commits, next_cursor

    def update_message(self, commit_id: int, new_message: str) -> bool:
        """Update the stored commit message (does not rewrite Git history)."""
        now = datetime.now(timezone.utc).isoformat()
        result = self.conn.execute(
            """
            UPDATE commits
            SET message = ?, created_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            [new_message, now, commit_id],
        )
        self.conn.commit()
        return result.fetchall()[0][0] > 0

    def soft_delete(self, commit_id: int) -> bool:
        """Mark a commit record as deleted."""
        now = datetime.now(timezone.utc).isoformat()
        result = self.conn.execute(
            """
            UPDATE commits
            SET deleted_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            [now, commit_id],
        )
        self.conn.commit()
        return result.fetchall()[0][0] > 0

    def _row_to_dict(self, row: tuple) -> dict[str, Any]:
        return {
            "id": row[0],
            "project_id": row[1],
            "hash": row[2],
            "message": row[3],
            "branch": row[4],
            "committed_at": row[5],
            "created_at": row[6],
            "deleted_at": row[7],
        }