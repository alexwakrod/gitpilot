"""Data access layer for commit patterns – user behavior learning."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlite3

logger = logging.getLogger(__name__)

ALLOWED_PATTERN_TYPES = {
    "message_style",
    "branch_prefix",
    "debounce_pref",
    "split_pref",
    "commit_frequency",
}


class PatternsRepository:
    """CRUD for learned user patterns (commit style, branch naming, etc.)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(
        self,
        owner: str,
        pattern_type: str,
        value: Any,
        confidence: float = 0.0,
    ) -> int:
        """Insert or update a pattern for the given owner and type.
        Returns the row ID.
        """
        if pattern_type not in ALLOWED_PATTERN_TYPES:
            raise ValueError(f"Invalid pattern_type: {pattern_type}")

        serialized = json.dumps(value)
        now = datetime.now(timezone.utc).isoformat()
        confidence = max(0.0, min(1.0, confidence))  # clamp

        existing = self.get_by_owner_and_type(owner, pattern_type)
        if existing:
            self.conn.execute(
                """
                UPDATE commit_patterns
                SET value = ?, confidence = ?, last_updated = ?
                WHERE owner = ? AND pattern_type = ?
                """,
                [serialized, confidence, now, owner, pattern_type],
            )
            return existing["id"]
        else:
            cursor = self.conn.execute(
                """
                INSERT INTO commit_patterns (owner, pattern_type, value, confidence, last_updated)
                VALUES (?, ?, ?, ?, ?)
                """,
                [owner, pattern_type, serialized, confidence, now],
            )
            return cursor.lastrowid

    def get_by_owner_and_type(
        self, owner: str, pattern_type: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a single pattern for the owner and type."""
        row = self.conn.execute(
            """
            SELECT id, owner, pattern_type, value, confidence, last_updated
            FROM commit_patterns
            WHERE owner = ? AND pattern_type = ?
            """,
            [owner, pattern_type],
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_by_owner(self, owner: str) -> List[Dict[str, Any]]:
        """Return all patterns for a given owner."""
        rows = self.conn.execute(
            """
            SELECT id, owner, pattern_type, value, confidence, last_updated
            FROM commit_patterns
            WHERE owner = ?
            ORDER BY pattern_type
            """,
            [owner],
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def decay_confidence(
        self, owner: str, pattern_type: Optional[str] = None
    ) -> int:
        """Reduce confidence for patterns that haven't been reinforced recently.
        Returns number of rows updated.
        """
        # Decrease confidence by 0.1 (bottom at 0.0) for patterns older than 7 days
        threshold = (
            datetime.now(timezone.utc).replace(microsecond=0) -
            sqlite3.dbapi2.timedelta(days=7)
        ).isoformat()

        if pattern_type:
            cursor = self.conn.execute(
                """
                UPDATE commit_patterns
                SET confidence = MAX(0.0, confidence - 0.1),
                    last_updated = ?
                WHERE owner = ?
                  AND pattern_type = ?
                  AND last_updated < ?
                  AND confidence > 0.0
                """,
                [datetime.now(timezone.utc).isoformat(), owner, pattern_type, threshold],
            )
        else:
            cursor = self.conn.execute(
                """
                UPDATE commit_patterns
                SET confidence = MAX(0.0, confidence - 0.1),
                    last_updated = ?
                WHERE owner = ?
                  AND last_updated < ?
                  AND confidence > 0.0
                """,
                [datetime.now(timezone.utc).isoformat(), owner, threshold],
            )
        updated = cursor.rowcount
        if updated:
            logger.info("Decayed confidence for %d patterns (owner=%s)", updated, owner)
        return updated

    def delete(self, pattern_id: int) -> bool:
        """Delete a pattern by ID."""
        cursor = self.conn.execute(
            "DELETE FROM commit_patterns WHERE id = ?",
            [pattern_id],
        )
        return cursor.rowcount > 0

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        try:
            data["value"] = json.loads(data["value"])
        except (json.JSONDecodeError, TypeError):
            pass
        return data