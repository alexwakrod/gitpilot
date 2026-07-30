"""Discord webhook notification client."""

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def send_discord_notification(
    webhook_url: str,
    project_name: str,
    commit_hash: str,
    message: str,
    branch: str,
    timestamp: str,
) -> bool:
    """Send a commit notification to a Discord webhook.
    Returns True on success, False on failure.
    """
    payload: dict[str, Any] = {
        "embeds": [
            {
                "title": f"New Commit in {project_name}",
                "color": 5814783,
                "fields": [
                    {"name": "Branch", "value": branch, "inline": True},
                    {"name": "Hash", "value": commit_hash[:8], "inline": True},
                    {"name": "Message", "value": message, "inline": False},
                ],
                "timestamp": timestamp,
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if response.status_code == 204:
                logger.info("Discord notification sent for commit %s", commit_hash[:8])
                return True
            logger.warning(
                "Discord webhook returned status %d: %s",
                response.status_code,
                response.text,
            )
            return False
    except httpx.HTTPError as exc:
        logger.error("Failed to send Discord notification: %s", exc)
        return False