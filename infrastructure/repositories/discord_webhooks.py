"""Data access layer for Discord webhook configurations."""

import logging
from datetime import datetime, timezone
from typing import Any

import duckdb

logger = logging.getLogger(__name__)


class DiscordWebhooksRepository:
    """CRUD operations for the discord_webhooks table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def create(self, project_id: int, url: str) -> int:
        """Add a Discord webhook for a project. Returns the new webhook ID."""
        now = datetime.now(timezone.utc).isoformat()
        result = self.conn.execute(
            """
            INSERT INTO discord_webhooks (project_id, url, enabled, created_at)
            VALUES (?, ?, TRUE, ?)
            RETURNING id
            """,
            [project_id, url, now],
        )
        webhook_id = result.fetchone()[0]
        self.conn.commit()
        logger.info("Discord webhook created id=%d for project_id=%d", webhook_id, project_id)
        return webhook_id

    def get_by_id(self, webhook_id: int) -> dict[str, Any] | None:
        """Retrieve a webhook by its ID (excluding soft-deleted)."""
        row = self.conn.execute(
            """
            SELECT id, project_id, url, enabled, created_at, deleted_at
            FROM discord_webhooks
            WHERE id = ? AND deleted_at IS NULL
            """,
            [webhook_id],
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_by_project(self, project_id: int) -> list[dict[str, Any]]:
        """Return all enabled webhooks for a project (excluding soft-deleted)."""
        rows = self.conn.execute(
            """
            SELECT id, project_id, url, enabled, created_at, deleted_at
            FROM discord_webhooks
            WHERE project_id = ? AND deleted_at IS NULL AND enabled = TRUE
            ORDER BY id ASC
            """,
            [project_id],
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def set_enabled(self, webhook_id: int, enabled: bool) -> bool:
        """Enable or disable a webhook. Returns True if a row was updated."""
        result = self.conn.execute(
            """
            UPDATE discord_webhooks
            SET enabled = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            [enabled, webhook_id],
        )
        self.conn.commit()
        return result.fetchall()[0][0] > 0

    def soft_delete(self, webhook_id: int) -> bool:
        """Soft-delete a webhook entry."""
        now = datetime.now(timezone.utc).isoformat()
        result = self.conn.execute(
            """
            UPDATE discord_webhooks
            SET deleted_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            [now, webhook_id],
        )
        self.conn.commit()
        return result.fetchall()[0][0] > 0

    def _row_to_dict(self, row: tuple) -> dict[str, Any]:
        return {
            "id": row[0],
            "project_id": row[1],
            "url": row[2],
            "enabled": row[3],
            "created_at": row[4],
            "deleted_at": row[5],
        }