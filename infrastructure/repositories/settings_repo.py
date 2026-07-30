"""Data access layer for settings."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import duckdb

logger = logging.getLogger(__name__)


class SettingsRepository:
    """CRUD operations for the settings table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get_all(self) -> dict[str, Any]:
        """Return all settings as a dictionary keyed by setting key."""
        rows = self.conn.execute(
            "SELECT key, value, type, updated_at FROM settings"
        ).fetchall()
        result = {}
        for row in rows:
            key = row[0]
            raw_value = row[1]  # DuckDB returns JSON as string
            value_type = row[2]
            try:
                parsed = json.loads(raw_value) if raw_value else None
            except json.JSONDecodeError:
                parsed = raw_value
            result[key] = {"value": parsed, "type": value_type, "updated_at": row[3]}
        return result

    def get_by_key(self, key: str) -> dict[str, Any] | None:
        """Retrieve a single setting by its key."""
        row = self.conn.execute(
            "SELECT key, value, type, updated_at FROM settings WHERE key = ?",
            [key],
        ).fetchone()
        if row is None:
            return None
        raw_value = row[1]
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed = raw_value
        return {"key": row[0], "value": parsed, "type": row[2], "updated_at": row[3]}

    def upsert(self, key: str, value: Any, value_type: str) -> None:
        """Insert or update a setting. The value is serialized to JSON."""
        valid_types = {"string", "integer", "boolean", "json"}
        if value_type not in valid_types:
            raise ValueError(f"Invalid setting type: {value_type}")

        serialized = json.dumps(value)
        now = datetime.now(timezone.utc).isoformat()

        existing = self.get_by_key(key)
        if existing:
            self.conn.execute(
                """
                UPDATE settings
                SET value = ?, type = ?, updated_at = ?
                WHERE key = ?
                """,
                [serialized, value_type, now, key],
            )
        else:
            self.conn.execute(
                """
                INSERT INTO settings (key, value, type, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                [key, serialized, value_type, now],
            )
        self.conn.commit()
        logger.info("Setting '%s' upserted", key)

    def delete(self, key: str) -> bool:
        """Delete a setting by key. Returns True if a row was removed."""
        result = self.conn.execute(
            "DELETE FROM settings WHERE key = ?", [key]
        )
        self.conn.commit()
        return result.fetchall()[0][0] > 0