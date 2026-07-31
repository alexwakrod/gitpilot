"""Data access layer for file associations – co‑occurrence tracking."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import sqlite3

logger = logging.getLogger(__name__)


class FileAssociationsRepository:
    """Tracks which files are commonly edited together."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record_co_occurrence(
        self,
        project_id: int,
        file_a: str,
        file_b: str,
    ) -> None:
        """Record that two files were modified in the same commit/debounce window.
        Increments the co‑occurrence counter.
        """
        # Normalize order to avoid duplicates: store the pair alphabetically
        if file_a > file_b:
            file_a, file_b = file_b, file_a

        now = datetime.now(timezone.utc).isoformat()
        existing = self.conn.execute(
            """
            SELECT id, co_occurrence FROM file_associations
            WHERE project_id = ? AND file_a = ? AND file_b = ?
            """,
            [project_id, file_a, file_b],
        ).fetchone()

        if existing:
            self.conn.execute(
                """
                UPDATE file_associations
                SET co_occurrence = co_occurrence + 1,
                    last_seen = ?
                WHERE id = ?
                """,
                [now, existing["id"]],
            )
        else:
            self.conn.execute(
                """
                INSERT INTO file_associations (project_id, file_a, file_b, last_seen)
                VALUES (?, ?, ?, ?)
                """,
                [project_id, file_a, file_b, now],
            )

    def get_associated_files(
        self,
        project_id: int,
        file_path: str,
        min_occurrences: int = 2,
        max_results: int = 10,
    ) -> List[Tuple[str, int]]:
        """Return files frequently edited together with file_path, sorted by co‑occurrence.
        Each element is (other_file, co_occurrence).
        """
        rows = self.conn.execute(
            """
            SELECT file_b, co_occurrence FROM file_associations
            WHERE project_id = ? AND file_a = ? AND co_occurrence >= ?
            UNION
            SELECT file_a, co_occurrence FROM file_associations
            WHERE project_id = ? AND file_b = ? AND co_occurrence >= ?
            ORDER BY co_occurrence DESC
            LIMIT ?
            """,
            [
                project_id, file_path, min_occurrences,
                project_id, file_path, min_occurrences,
                max_results,
            ],
        ).fetchall()
        return [(row["file_b"], row["co_occurrence"]) for row in rows]

    def prune_stale(self, project_id: int, max_age_days: int = 30) -> int:
        """Remove associations not seen for `max_age_days`. Returns count removed."""
        threshold = (
            datetime.now(timezone.utc).replace(microsecond=0) -
            sqlite3.dbapi2.timedelta(days=max_age_days)
        ).isoformat()
        cursor = self.conn.execute(
            """
            DELETE FROM file_associations
            WHERE project_id = ? AND last_seen < ?
            """,
            [project_id, threshold],
        )
        removed = cursor.rowcount
        if removed:
            logger.info("Pruned %d stale file associations for project %d", removed, project_id)
        return removed

    def delete_for_project(self, project_id: int) -> int:
        """Remove all associations for a project. Returns count removed."""
        cursor = self.conn.execute(
            "DELETE FROM file_associations WHERE project_id = ?",
            [project_id],
        )
        return cursor.rowcount