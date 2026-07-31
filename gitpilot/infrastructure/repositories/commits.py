"""Data access layer for commits – SQLite backend with domain metadata."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import sqlite3

logger = logging.getLogger(__name__)


class CommitsRepository:
    """CRUD operations for the commits table."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(
        self,
        project_id: int,
        hash: str,
        message: str,
        branch: str = "main",
        domain: str = "general",
        affected_symbols: Optional[List[str]] = None,
        optimization_notes: Optional[List[str]] = None,
        committed_at: Optional[str] = None,
    ) -> int:
        """Insert a new commit record and return its ID."""
        if committed_at is None:
            committed_at = datetime.now(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()

        symbols_json = json.dumps(affected_symbols or [])
        optimizations_json = json.dumps(optimization_notes or [])

        cursor = self.conn.execute(
            """
            INSERT INTO commits (
                project_id, hash, message, branch, domain,
                affected_symbols, optimization_notes, committed_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                project_id, hash, message, branch, domain,
                symbols_json, optimizations_json, committed_at, now,
            ],
        )
        commit_id = cursor.lastrowid
        logger.info("Recorded commit id=%d hash=%s domain=%s", commit_id, hash[:8], domain)
        return commit_id

    def get_by_id(self, commit_id: int) -> Optional[dict[str, Any]]:
        """Retrieve a commit by ID (excluding soft-deleted)."""
        row = self.conn.execute(
            """
            SELECT id, project_id, hash, message, branch, domain,
                   affected_symbols, optimization_notes, squash_candidate,
                   committed_at, created_at, deleted_at
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
        cursor: Optional[str] = None,
        domain_filter: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
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

        domain_cond = ""
        if domain_filter:
            domain_cond = " AND domain = ?"
            params.append(domain_filter)

        query = f"""
            SELECT id, project_id, hash, message, branch, domain,
                   affected_symbols, optimization_notes, squash_candidate,
                   committed_at, created_at, deleted_at
            FROM commits
            WHERE project_id = ? AND deleted_at IS NULL{cursor_cond}{domain_cond}
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
        cursor = self.conn.execute(
            """
            UPDATE commits
            SET message = ?, created_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            [new_message, now, commit_id],
        )
        return cursor.rowcount > 0

    def soft_delete(self, commit_id: int) -> bool:
        """Mark a commit record as deleted."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE commits
            SET deleted_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            [now, commit_id],
        )
        return cursor.rowcount > 0

    def mark_squash_candidates(
        self,
        project_id: int,
        branch: str,
        domain: str,
        max_age_minutes: int = 10,
    ) -> int:
        """Mark recent commits in the same branch+domain as squash candidates.
        Returns number of commits updated (int, not bool).
        """
        threshold = (
            datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        ).isoformat()

        cursor = self.conn.execute(
            """
            UPDATE commits
            SET squash_candidate = TRUE
            WHERE project_id = ?
              AND branch = ?
              AND domain = ?
              AND deleted_at IS NULL
              AND committed_at >= ?
              AND squash_candidate = FALSE
            """,
            [project_id, branch, domain, threshold],
        )
        return cursor.rowcount

    def clear_squash_candidates(
        self,
        project_id: int,
        branch: str,
        domain: Optional[str] = None,
    ) -> int:
        """Clear squash candidate flags."""
        params = [project_id, branch]
        domain_cond = ""
        if domain:
            domain_cond = " AND domain = ?"
            params.append(domain)

        cursor = self.conn.execute(
            f"""
            UPDATE commits
            SET squash_candidate = FALSE
            WHERE project_id = ?
              AND branch = ?
              AND deleted_at IS NULL{domain_cond}
            """,
            params,
        )
        return cursor.rowcount

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        # Convert integer flag to Python bool
        if "squash_candidate" in data and isinstance(data["squash_candidate"], int):
            data["squash_candidate"] = bool(data["squash_candidate"])
        if data.get("affected_symbols"):
            try:
                data["affected_symbols"] = json.loads(data["affected_symbols"])
            except (json.JSONDecodeError, TypeError):
                data["affected_symbols"] = []
        if data.get("optimization_notes"):
            try:
                data["optimization_notes"] = json.loads(data["optimization_notes"])
            except (json.JSONDecodeError, TypeError):
                data["optimization_notes"] = []
        return data